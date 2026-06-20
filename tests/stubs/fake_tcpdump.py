"""Cross-platform fake `tcpdump`: parse `-w <path>`, create the file, then sleep
until terminated. Lets CaptureManager tests run with no real tcpdump (Windows)."""
import sys
import time


def main(argv):
    path = None
    for i, a in enumerate(argv):
        if a == "-w" and i + 1 < len(argv):
            path = argv[i + 1]
    if path:
        with open(path, "wb") as fh:
            fh.write(b"\xd4\xc3\xb2\xa1")  # pcap magic; enough for a real file
    try:
        while True:
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
