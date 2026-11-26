"""关怀服务启动入口：python -m care --port 8080。"""

from __future__ import annotations

import argparse

from .api import CareServer, build_default_tokens
from .storage import JsonStore


def main() -> None:
    parser = argparse.ArgumentParser(description="通信关怀服务后端（仅本地回环）")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--store", default="data/care_snapshot.json", help="快照文件路径")
    parser.add_argument("--feed", default="http://127.0.0.1:8765", help="本地资料端地址")
    parser.add_argument("--no-store", action="store_true", help="不读写快照，纯内存运行")
    parser.add_argument("--sync-once", action="store_true", help="启动后同步一次资料端再退出")
    args = parser.parse_args()

    store = None if args.no_store else JsonStore(args.store)
    server = CareServer(
        args.port,
        tokens=build_default_tokens(),
        store=store,
        feed_url=args.feed,
    )
    if args.sync_once:
        print(server.sync_feed(), flush=True)
        server.save()
        server.server_close()
        return
    try:
        with server:
            print(f"关怀服务 http://127.0.0.1:{server.server_port}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.save()


if __name__ == "__main__":
    main()
