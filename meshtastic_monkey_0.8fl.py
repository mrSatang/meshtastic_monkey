#!/usr/bin/env python3
import threading
import time
import queue
from datetime import datetime
from meshtastic.serial_interface import SerialInterface
from pubsub import pub
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory
import os

C_BLUE = "\033[94m"
C_GREEN = "\033[32m"
C_RED = "\033[91m"
C_RESET = "\033[0m"

ACK_TIMEOUT = 30
MAX_PENDING_ACKS = 100
SEND_DELAY = 1.5  # Задержка между отправками для избежания сбоев в доставке

stop_event = threading.Event()
pending_acks = {}
pending_lock = threading.Lock()
name_cache = {}
name_cache_lock = threading.Lock()
send_queue = queue.Queue(maxsize=30)

def ts():
    return datetime.now().strftime("%m-%d %H:%M")

def normalize_node_id(raw_id):
    if isinstance(raw_id, str) and raw_id.startswith("!"):
        try:
            return int(raw_id[1:], 16)
        except ValueError:
            return None
    if isinstance(raw_id, int):
        return raw_id
    return None

def get_name_and_id(interface, node_id):
    if node_id is None or not isinstance(node_id, int):
        return "unknown", "@unknown"
    with name_cache_lock:
        if node_id not in name_cache:
            node = interface.nodes.get(node_id, {})
            user = node.get("user", {})
            short = user.get("shortName")
            if short:
                name_cache[node_id] = short
                display = f"{short} (@!{node_id:08x})"
            else:
                hex_key = f"!{node_id:08x}"
                node_by_hex = interface.nodes.get(hex_key)
                if node_by_hex:
                    short = node_by_hex.get("user", {}).get("shortName")
                    if short:
                        name_cache[node_id] = short
                        display = f"{short} (@{hex_key})"
                    else:
                        name_cache[node_id] = hex_key
                        display = f"{hex_key} (@!{node_id:08x})"
                else:
                    name_cache[node_id] = hex_key
                    display = f"{hex_key} (@!{node_id:08x})"
        else:
            name = name_cache[node_id]
            display = f"{name} (@!{node_id:08x})"
    return name_cache.get(node_id, "unknown"), display

def resolve_node(interface, token):
    if token.startswith("!"):
        try:
            return int(token[1:], 16)
        except ValueError:
            return None
    nodes_snapshot = list(interface.nodes.items())
    for nid, node in nodes_snapshot:
        user = node.get("user", {})
        if user.get("shortName") == token:
            if isinstance(nid, int):
                return nid
            if isinstance(nid, str) and nid.startswith("!"):
                try:
                    return int(nid[1:], 16)
                except ValueError:
                    pass
    return None

def on_node_updated(node):
    nid = node.get("num")
    if nid is not None:
        with name_cache_lock:
            name_cache.pop(nid, None)

def sender_thread(interface, stop_event, send_queue, pending_acks, pending_lock):
    while not stop_event.is_set():
        try:
            task = send_queue.get(timeout=1)
            if stop_event.is_set():
                break
            text, dest_id, want_ack = task
            try:
                if dest_id is not None:
                    pkt = interface.sendText(text, destinationId=dest_id, wantAck=want_ack)
                else:
                    pkt = interface.sendText(text, wantAck=want_ack)
                if want_ack and pkt and hasattr(pkt, 'id'):
                    with pending_lock:
                        if len(pending_acks) >= MAX_PENDING_ACKS:
                            oldest = next(iter(pending_acks))
                            pending_acks.pop(oldest, None)
                            with patch_stdout(raw=True):
                                print(f"{C_BLUE}[{ts()}] WARN pending_acks overflow, dropping oldest{C_RESET}")
                        pending_acks[pkt.id] = (time.time(), dest_id)
                time.sleep(SEND_DELAY)  # Задержка для стабильной доставки
            except Exception as send_e:
                with patch_stdout(raw=True):
                    print(f"TX error: {send_e}")
        except queue.Empty:
            continue
        except Exception as e:
            with patch_stdout(raw=True):
                print(f"Sender thread error: {e}")

def on_receive(packet, interface):
    try:
        decoded = packet.get("decoded", {})
        port = decoded.get("portnum")
        if port == "ROUTING_APP":
            req_id = decoded.get("requestId")
            if req_id is None:
                return
            with pending_lock:
                entry = pending_acks.pop(req_id, None)
            if entry:
                sent_at, _ = entry
                rtt = time.time() - sent_at
                from_id = normalize_node_id(packet.get("fromId") or packet.get("from"))
                _, display = get_name_and_id(interface, from_id) if from_id else ("?", "unknown")
                rssi = packet.get("rxRssi")
                snr = packet.get("rxSnr")
                params = [f"RTT={rtt:.1f}s"]
                if rssi is not None:
                    params.append(f"RSSI={rssi}")
                if snr is not None:
                    params.append(f"SNR={snr}")
                with patch_stdout(raw=True):
                    print(f"{C_BLUE}[{ts()}] ACK from {display}, " + ", ".join(params) + f"{C_RESET}")
            return
        if port != "TEXT_MESSAGE_APP":
            return
        text = decoded.get("payload", b"").decode(errors="ignore").strip()
        from_id = normalize_node_id(packet.get("fromId") or packet.get("from"))
        name, _ = get_name_and_id(interface, from_id) if from_id else ("unknown", "")
        display_id = f"@!{from_id:08x}" if from_id else "@??"
        try:
            my_id = interface.myInfo.my_node_num
        except AttributeError:
            my_id = None
        to_id = normalize_node_id(packet.get("to"))
        is_private = to_id not in (None, 0xFFFFFFFF) and to_id == my_id
        rssi = packet.get("rxRssi")
        snr = packet.get("rxSnr")
        hop_start = packet.get("hopStart")
        hop_limit = packet.get("hopLimit")
        params = []
        if rssi is not None:
            params.append(f"RSSI={rssi}")
        if snr is not None:
            params.append(f"SNR={snr}")
        if hop_start and hop_limit:
            params.append(f"hops={hop_start - hop_limit}")
        meta = f"({display_id}, {', '.join(params)})" if params else f"({display_id})"
        with patch_stdout(raw=True):
            if is_private:
                print(f"{C_RED}[{ts()}] PM {name}: {text}{C_RESET}")
            else:
                print(f"{C_GREEN}[{ts()}] PUB {name}: {text}{C_RESET}")
            print(f"{C_BLUE}{meta}{C_RESET}")
        import re
        markers = ["testa", "test", "тест", "ping"]  # список маркеров
        pattern = r"\b(?:" + "|".join(re.escape(marker) for marker in markers) + r")\b"
        if re.search(pattern, text, re.IGNORECASE):
            ts_full = ts()              # "02-06 16:43"
            ts_str = ts_full.split()[1]  # "16:43"
            tech_data = ', '.join(params)
            reply_text = f"✅ AR-ACK [{ts_str}] {name} ({display_id}, 📶{tech_data})"
            dest_id = from_id if is_private else None
            try:
                send_queue.put_nowait((reply_text, dest_id, False))
            except queue.Full:
                with patch_stdout(raw=True):
                    print(f"{C_BLUE}[{ts()}] Send queue full, dropping auto reply{C_RESET}")
    except Exception as e:
        with patch_stdout(raw=True):
            print(f"RX error: {e}")

class MeshtasticCompleter(Completer):
    def __init__(self, interface):
        self.interface = interface

    def get_completions(self, document, complete_event):
        words = []
        short_names = set()
        nodes_snapshot = list(self.interface.nodes.items())
        for nid, node in nodes_snapshot:
            user = node.get("user", {})
            short = user.get("shortName")
            if short:
                short_names.add(short)
        with name_cache_lock:
            for short in list(name_cache.values()):
                if isinstance(short, str) and not short.startswith("!"):
                    short_names.add(short)
        words.extend(f"@{name}" for name in sorted(short_names))
        text_before_cursor = document.text_before_cursor
        for word in words:
            if word.startswith(text_before_cursor):
                yield Completion(word, start_position=-len(text_before_cursor))

def main():
    try:
        interface = SerialInterface()
    except Exception as e:
        print(f"Meshtastic device not found: {e}")
        return

    history_file = os.path.expanduser("~/.meshtastic_history")
    history = FileHistory(history_file)

    def get_bottom_toolbar():
        try:
            text = session.default_buffer.text
        except AttributeError:
            text = ""
        char_count = len(text)
        byte_count = len(text.encode('utf-8'))
        return f"chars: {char_count}, bytes: {byte_count}"

    session = PromptSession(
        history=history,
        completer=MeshtasticCompleter(interface),
        bottom_toolbar=get_bottom_toolbar,
    )

    def input_loop():
        while not stop_event.is_set():
            try:
                with patch_stdout(raw=True):
                    raw_line = session.prompt("")
                line = raw_line.strip()
                if not line:
                    continue
                dest_id = None
                text = line
                if line.startswith("@"):
                    parts = line.split(maxsplit=1)
                    target = parts[0][1:]
                    text = parts[1] if len(parts) > 1 else ""
                    dest_id = resolve_node(interface, target)
                    if dest_id is None:
                        with patch_stdout(raw=True):
                            print(f"{C_BLUE}[{ts()}] Unknown node: {target}{C_RESET}")
                        continue
                try:
                    send_queue.put_nowait((text, dest_id, True))
                except queue.Full:
                    with patch_stdout(raw=True):
                        print(f"{C_BLUE}[{ts()}] Send queue full, dropping message{C_RESET}")
            except EOFError:
                pass
            except KeyboardInterrupt:
                stop_event.set()
                break
            except Exception as e:
                with patch_stdout(raw=True):
                    print(f"Input error: {e}")
                time.sleep(0.1)

    def ack_watcher():
        while not stop_event.is_set():
            time.sleep(1)
            now = time.time()
            expired = []
            with pending_lock:
                for rid, (t0, dest) in list(pending_acks.items()):
                    if now - t0 > ACK_TIMEOUT:
                        expired.append((rid, dest))
                        pending_acks.pop(rid, None)
            for rid, dest in expired:
                dst = f"@!{dest:08x}" if dest else "@broadcast"
                with patch_stdout(raw=True):
                    print(f"{C_BLUE}[{ts()}] NO ACK from {dst}, id={rid}{C_RESET}")

    sender = threading.Thread(
        target=sender_thread,
        args=(interface, stop_event, send_queue, pending_acks, pending_lock),
        daemon=True
    )
    sender.start()

    pub.subscribe(on_receive, "meshtastic.receive")
    pub.subscribe(on_node_updated, "meshtastic.node.updated")

    threading.Thread(target=input_loop, daemon=True).start()
    threading.Thread(target=ack_watcher, daemon=True).start()

    try:
        with patch_stdout(raw=True):
            print(f"{C_BLUE}Meshtastic chat started. All lines sent immediately. Ctrl+C to exit.{C_RESET}")
            print(f"{C_BLUE}Use Tab for auto-completion of node names.{C_RESET}")
            print(f"{C_BLUE}Interactive char/byte counter shown at bottom.{C_RESET}")
        while not stop_event.is_set():
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        interface.close()
        with patch_stdout(raw=True):
            print(f"\n{C_BLUE}Stopped.{C_RESET}")

if __name__ == "__main__":
    main()
