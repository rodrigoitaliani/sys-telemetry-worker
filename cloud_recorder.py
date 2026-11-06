import argparse
import datetime
import io
import os
import socket
import subprocess
import sys
import time
import urllib.request

import av
from PIL import Image

TUNNEL_PORT = 18089


def log(msg):
    # Horario de Brasilia (UTC-3)
    tz_br = datetime.timezone(datetime.timedelta(hours=-3))
    ts = datetime.datetime.now(tz_br).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts} BRT] {msg}", flush=True)


def is_host_online(host):
    try:
        with socket.create_connection((host, 22), timeout=4.0):
            return True
    except Exception:
        return False


class CloudTunnelWatchdog:
    def __init__(self, target_host, target_user, ssh_key_path, port):
        self.host = target_host
        self.user = target_user
        self.key = ssh_key_path
        self.port = port
        self.proc = None

    def start_or_heal(self, force=False, wake_task=False):
        if not force and self.proc is not None and self.proc.poll() is None:
            return True

        self.stop()
        log("Watchdog: Abrindo tunel SSH 100% passivo somente-leitura (-N -L)...")
        ssh_target = f"{self.user}@{self.host}"
        base_ssh = [
            "ssh",
            "-i", self.key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=6",
            "-o", "BatchMode=yes",
        ]

        try:
            self.proc = subprocess.Popen(
                base_ssh + [
                    "-N",
                    "-L", f"127.0.0.1:{self.port}:127.0.0.1:8088",
                    "-o", "ServerAliveInterval=10",
                    "-o", "ServerAliveCountMax=2",
                    "-o", "ExitOnForwardFailure=yes",
                    ssh_target,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1.5)
            return self.proc.poll() is None
        except Exception as e:
            log(f"Watchdog: Erro ao iniciar tunel: {e}")
            return False

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None


def fetch_frame(port, quality=70):
    url = f"http://127.0.0.1:{port}/frame.jpg?all=1&q={quality}&t={int(time.time()*1000)}"
    with urllib.request.urlopen(url, timeout=4.5) as resp:
        data = resp.read()
    im = Image.open(io.BytesIO(data))
    im.load()
    return im.convert("RGB") if im.mode != "RGB" else im


def upload_to_gdrive(local_file, day_str):
    remote_dest = f"gdrive:CCTV_Vivobook/{day_str}/"
    log(f"Enviando {os.path.basename(local_file)} para Google Drive ({remote_dest})...")
    try:
        res = subprocess.run(
            ["rclone", "copy", local_file, remote_dest, "--transfers=1"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if res.returncode == 0:
            log(f"Upload concluido com sucesso no Google Drive: {os.path.basename(local_file)}")
            try:
                os.remove(local_file)
            except Exception:
                pass
        else:
            log(f"Aviso no upload rclone: {res.stderr.strip()}")
    except Exception as e:
        log(f"Erro ao executar rclone: {e}")


def record_block(watchdog, host, block_seconds, fps=2, crf="28"):
    tz_br = datetime.timezone(datetime.timedelta(hours=-3))
    now_br = datetime.datetime.now(tz_br)
    day_str = now_br.strftime("%Y-%m-%d")
    out_dir = os.path.join("recordings", day_str)
    os.makedirs(out_dir, exist_ok=True)

    first_img = None
    for attempt in range(4):
        try:
            watchdog.start_or_heal(force=(attempt > 0), wake_task=(attempt >= 2))
            first_img = fetch_frame(TUNNEL_PORT)
            break
        except Exception:
            time.sleep(2.0)

    if first_img is None:
        log("Nao foi possivel obter o quadro inicial. Aguardando...")
        return False

    target_w = first_img.width - (first_img.width % 2)
    target_h = first_img.height - (first_img.height % 2)
    first_img.close()

    fname = f"Vivobook_{now_br.strftime('%Y-%m-%d_%Hh%Mm%Ss')}.mp4"
    video_path = os.path.join(out_dir, fname)
    log(f"Iniciando bloco MP4: {fname} ({target_w}x{target_h} @ {fps} FPS)")

    container = av.open(video_path, mode="w")
    stream = container.add_stream("libx264", rate=fps)
    stream.width = target_w
    stream.height = target_h
    stream.pix_fmt = "yuv420p"
    stream.options = {"crf": crf, "preset": "veryfast", "tune": "stillimage", "g": str(fps * 30)}

    t_start = time.monotonic()
    frames = 0
    fails = 0
    interval = 1.0 / fps

    try:
        while time.monotonic() - t_start < block_seconds:
            t0 = time.monotonic()
            try:
                im = fetch_frame(TUNNEL_PORT)
                fails = 0
                if im.width != target_w or im.height != target_h:
                    res = im.resize((target_w, target_h), Image.Resampling.BILINEAR)
                    im.close()
                    im = res

                vf = av.VideoFrame.from_image(im)
                im.close()
                for pkt in stream.encode(vf):
                    container.mux(pkt)
                frames += 1

                if frames % (fps * 60) == 0:
                    mb = os.path.getsize(video_path) / (1024 * 1024)
                    el_m = int((time.monotonic() - t_start) // 60)
                    log(f"Progresso: {el_m} min gravados | {frames} quadros | {mb:.2f} MB")

            except Exception:
                fails += 1
                if fails >= 3:
                    log("Watchdog: Falha na captura. Verificando conectividade e recriando tunel...")
                    if not is_host_online(host):
                        log("Maquina remota ficou offline. Finalizando bloco atual...")
                        break
                    watchdog.start_or_heal(force=True)
                    fails = 0

            dt = time.monotonic() - t0
            time.sleep(max(0.02, interval - dt))
    finally:
        try:
            for pkt in stream.encode():
                container.mux(pkt)
            container.close()
        except Exception:
            pass

    if os.path.exists(video_path) and frames > 0:
        mb = os.path.getsize(video_path) / (1024 * 1024)
        log(f"Bloco finalizado: {fname} ({mb:.2f} MB, {frames} quadros)")
        upload_to_gdrive(video_path, day_str)
        return True
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-minutes", type=int, default=60)
    args = parser.parse_args()

    host = os.environ.get("TARGET_HOST", "").strip()
    user = os.environ.get("TARGET_USER", "").strip()
    ssh_key = os.environ.get("SSH_KEY_PATH", os.path.expanduser("~/.ssh/id_target"))

    if not host or not user:
        log("ERRO: Variaveis TARGET_HOST / TARGET_USER nao definidas.")
        sys.exit(1)

    total_seconds = args.duration_minutes * 60
    session_start = time.monotonic()
    watchdog = CloudTunnelWatchdog(host, user, ssh_key, TUNNEL_PORT)

    log(f"=== Sessao Cloud DVR Iniciada (Duracao planejada: {args.duration_minutes} min) ===")
    try:
        while True:
            elapsed = time.monotonic() - session_start
            remaining = total_seconds - elapsed
            if remaining <= 30:
                log("Tempo total da sessao concluido.")
                break

            if not is_host_online(host):
                watchdog.stop()
                log("Maquina remota offline no momento. Checando novamente em 60s...")
                time.sleep(60)
                continue

            # Grava em blocos de ate 60 minutos por arquivo .mp4
            block_sec = min(3600, int(remaining))
            record_block(watchdog, host, block_sec)
            time.sleep(2)
    finally:
        watchdog.stop()
        log("=== Sessao Cloud DVR Encerrada ===")


if __name__ == "__main__":
    main()
