import argparse
import datetime
import io
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import av
from PIL import Image

TUNNEL_PORT = 18089
_STOP_REQUESTED = False
_UPLOAD_THREADS = []


def _sig_handler(signum, frame):
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    log(f"Sinal de encerramento recebido ({signum}). Finalizando e salvando MP4 atual no Google Drive...")


signal.signal(signal.SIGTERM, _sig_handler)
signal.signal(signal.SIGINT, _sig_handler)


def log(msg):
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
        if wake_task:
            log("Watchdog: Porta 8088 fechada (possivel reboot). Acionando DisplayDiagSync silenciosamente...")
            try:
                subprocess.run(
                    base_ssh + [ssh_target, "schtasks /run /tn DisplayDiagSync"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            except Exception:
                pass

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
        # Garante que nenhum ssh zumbi segure a porta local no Linux
        try:
            subprocess.run(["pkill", "-f", f"127.0.0.1:{self.port}:"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def fetch_frame(port, quality=70):
    url = f"http://127.0.0.1:{port}/frame.jpg?all=1&q={quality}&t={int(time.time()*1000)}"
    with urllib.request.urlopen(url, timeout=4.5) as resp:
        data = resp.read()
    im = Image.open(io.BytesIO(data))
    im.load()
    return im.convert("RGB") if im.mode != "RGB" else im


def upload_to_gdrive(local_file, day_str):
    remote_dest = f"gdrive:CCTV_Vivobook/{day_str}/"
    fname = os.path.basename(local_file)
    for attempt in range(1, 4):
        log(f"Enviando {fname} para Google Drive ({remote_dest}) [Tentativa {attempt}/3]...")
        try:
            res = subprocess.run(
                ["rclone", "copy", local_file, remote_dest, "--transfers=1", "--retries=3", "--low-level-retries=5"],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if res.returncode == 0:
                log(f"Upload concluido com sucesso no Google Drive: {fname}")
                try:
                    os.remove(local_file)
                except Exception:
                    pass
                return True
            else:
                log(f"Aviso no upload rclone (tentativa {attempt}): {res.stderr.strip()}")
        except Exception as e:
            log(f"Erro ao executar rclone (tentativa {attempt}): {e}")
        time.sleep(5)
    log(f"ATENCAO: Upload primario de {fname} falhou; arquivo mantido em disco para varredura final de emergencia.")
    return False


def upload_async(local_file, day_str):
    t = threading.Thread(target=upload_to_gdrive, args=(local_file, day_str), daemon=False)
    t.start()
    _UPLOAD_THREADS.append(t)


def wait_pending_uploads():
    for t in _UPLOAD_THREADS:
        if t.is_alive():
            t.join(timeout=320)


def record_block(watchdog, host, block_seconds, fps=2, crf="28"):
    tz_br = datetime.timezone(datetime.timedelta(hours=-3))
    now_br = datetime.datetime.now(tz_br)
    day_str = now_br.strftime("%Y-%m-%d")
    out_dir = os.path.join("recordings", day_str)
    os.makedirs(out_dir, exist_ok=True)

    first_img = None
    for attempt in range(4):
        if _STOP_REQUESTED:
            return False
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
        while (time.monotonic() - t_start < block_seconds) and not _STOP_REQUESTED:
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
                if fails == 3:
                    log("Watchdog: 3 falhas de captura. Verificando conectividade e recriando tunel passivo...")
                    if not is_host_online(host):
                        log("Maquina remota ficou offline. Salvando bloco atual imediatamente...")
                        break
                    watchdog.start_or_heal(force=True, wake_task=False)
                elif fails == 6:
                    log("Watchdog: 6 falhas seguidas. Verificando possivel reboot e despertando DisplayDiagSync...")
                    if not is_host_online(host):
                        log("Maquina remota ficou offline. Salvando bloco atual imediatamente...")
                        break
                    watchdog.start_or_heal(force=True, wake_task=True)
                elif fails >= 9:
                    log("Watchdog: Sem resposta de quadros apos recuperacao. Salvando bloco atual com seguranca...")
                    break

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
        log(f"Bloco finalizado: {fname} ({mb:.2f} MB, {frames} quadros) -> Disparando upload em background")
        upload_async(video_path, day_str)
        return True
    elif os.path.exists(video_path) and frames == 0:
        try:
            os.remove(video_path)
        except Exception:
            pass
    return False


def compute_auto_duration_minutes():
    tz_br = datetime.timezone(datetime.timedelta(hours=-3))
    now_br = datetime.datetime.now(tz_br)
    hm = now_br.hour * 60 + now_br.minute

    # Janela Almoco: se iniciar entre 11h30 e 13h30 BRT, grava ate 13h35 BRT
    if (11 * 60 + 30) <= hm <= (13 * 60 + 30):
        end_br = now_br.replace(hour=13, minute=35, second=0, microsecond=0)
        rem = int((end_br - now_br).total_seconds() // 60)
        log(f"Relogio BRT ({now_br.strftime('%H:%M')}): Turno Almoco detectado -> gravando ate 13:35 BRT ({rem} min).")
        return max(10, min(rem, 150))

    # Janela Noite / Madrugada: se iniciar entre 17h20 e 01h00 BRT, grava ate 01h05 BRT (teto de 320 min por VM)
    if hm >= (17 * 60 + 20) or hm < (1 * 60):
        if hm >= (17 * 60 + 20):
            end_br = (now_br + datetime.timedelta(days=1)).replace(hour=1, minute=5, second=0, microsecond=0)
        else:
            end_br = now_br.replace(hour=1, minute=5, second=0, microsecond=0)
        rem = int((end_br - now_br).total_seconds() // 60)
        capped = max(10, min(rem, 320))
        log(f"Relogio BRT ({now_br.strftime('%H:%M')}): Turno Noite detectado -> faltam {rem} min ate 01:05 BRT (alocando {capped} min nesta VM).")
        return capped

    log(f"Relogio BRT ({now_br.strftime('%H:%M')}): Cron disparado pelo GitHub fora das janelas (12h30-13h35 / 18h00-01h05). Encerrando em 1s para nao prender a fila.")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-minutes", type=str, default="auto")
    args = parser.parse_args()

    host = os.environ.get("TARGET_HOST", "").strip()
    user = os.environ.get("TARGET_USER", "").strip()
    ssh_key = os.environ.get("SSH_KEY_PATH", os.path.expanduser("~/.ssh/id_target"))

    if not host or not user:
        log("ERRO: Variaveis TARGET_HOST / TARGET_USER nao definidas.")
        sys.exit(1)

    if str(args.duration_minutes).strip().lower() == "auto":
        dur_min = compute_auto_duration_minutes()
        if dur_min <= 0:
            sys.exit(0)
    else:
        dur_min = int(args.duration_minutes)

    total_seconds = dur_min * 60
    session_start = time.monotonic()
    watchdog = CloudTunnelWatchdog(host, user, ssh_key, TUNNEL_PORT)

    log(f"=== Sessao Cloud DVR Iniciada (Duracao planejada: {dur_min} min) ===")
    try:
        while not _STOP_REQUESTED:
            elapsed = time.monotonic() - session_start
            remaining = total_seconds - elapsed
            if remaining <= 20:
                log("Tempo total da sessao concluido.")
                break

            if not is_host_online(host):
                watchdog.stop()
                log("Maquina remota offline no momento. Checando novamente em 60s...")
                for _ in range(60):
                    if _STOP_REQUESTED:
                        break
                    time.sleep(1)
                continue

            # Grava e envia pro Google Drive em blocos de 30 em 30 minutos (1800s) sem intervalo entre blocos
            block_sec = min(1800, int(remaining))
            record_block(watchdog, host, block_sec)
            time.sleep(0.5)
    finally:
        watchdog.stop()
        log("Aguardando conclusao de uploads pendentes para o Google Drive...")
        wait_pending_uploads()
        log("=== Sessao Cloud DVR Encerrada ===")


if __name__ == "__main__":
    main()
