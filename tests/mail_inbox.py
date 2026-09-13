"""模拟邮箱投递命令：把邮件放进收件箱目录，等于这一刻收到一封新邮件。

    python -m tests.mail_inbox list                     # 列出样例邮件
    python -m tests.mail_inbox deliver invite           # 投递样例，可跟多个名字或 json 路径
    python -m tests.mail_inbox compose --from ... \\     # 现写一封投进去
        --subject ... --body "第一段\\n\\n第二段"
    python -m tests.mail_inbox clear                    # 清空收件箱目录

收件箱默认 `<数据目录>/inbox`，与 `tests.support.manual_backend` 一致，可用
`PEBBLE_TEST_INBOX_DIR` 指定；直接往这个目录里放符合字段的 json 也算投递。
邮件 ID 决定去重：重复投递同一个 ID 不会重复建任务，同线程的新 ID 是同线程的另一封邮件。
"""

import argparse
import json
import shutil
import time
from pathlib import Path

from tests.support.mailbox import inbox_dir, load_mail

MAILS = Path(__file__).resolve().parent / "support" / "mails"


def templates() -> dict[str, Path]:
    return {path.stem: path for path in sorted(MAILS.glob("*.json"))}


def target_dir() -> Path:
    target = inbox_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target


def announce(path: Path, mail: dict) -> None:
    print(f"已投递 → {path}")
    print(f"  邮件 ID {mail['message_id']} · 线程 {mail['thread_id']}")
    print(f"  {mail['from']} · {mail['subject']}")


def show_list(_: argparse.Namespace) -> int:
    print(f"样例邮件目录：{MAILS}")
    for name, path in templates().items():
        mail = load_mail(path)
        print(f"  {name:<12} {mail['from']:<24} {mail['subject']}")
    print(f"收件箱目录：{inbox_dir()}")
    return 0


def deliver(args: argparse.Namespace) -> int:
    available = templates()
    for name in args.names:
        source = available.get(name) or Path(name)
        if not source.is_file():
            print(f"找不到这封邮件：{name}；样例可选 {', '.join(available)}，也可以给 json 路径")
            return 2
        mail = load_mail(source)
        path = target_dir() / source.name
        shutil.copyfile(source, path)
        announce(path, mail)
    return 0


def compose(args: argparse.Namespace) -> int:
    """现写一封邮件投进收件箱；只有发件人、主题和正文是必填。"""
    message_id = args.id or f"mail-{time.strftime('%Y%m%d-%H%M%S')}"
    mail = {
        "message_id": message_id,
        "thread_id": args.thread or f"thread-{message_id}",
        "from": getattr(args, "from"),
        "to": [args.to],
        "subject": args.subject,
        "received_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        # 命令行里的 \n 当换行处理，方便写多段正文。
        "body": args.body.replace("\\n", "\n"),
    }
    for key in ("summary", "suggestion", "reply_body"):
        value = getattr(args, key)
        if value:
            mail[key] = value.replace("\\n", "\n")
    path = target_dir() / f"{message_id}.json"
    path.write_text(json.dumps(mail, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    announce(path, mail)
    return 0


def clear(_: argparse.Namespace) -> int:
    target = inbox_dir()
    removed = sorted(target.glob("*.json")) if target.is_dir() else []
    for path in removed:
        path.unlink()
    print(f"已清空 {target}，移除 {len(removed)} 封；已创建的任务和草稿不受影响。")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m tests.mail_inbox", description=__doc__)
    commands = root.add_subparsers(dest="command")

    commands.add_parser("list", help="列出样例邮件与收件箱目录").set_defaults(run=show_list)

    send = commands.add_parser("deliver", help="投递样例邮件或指定 json 文件")
    send.add_argument("names", nargs="+", help="样例名字或 json 路径")
    send.set_defaults(run=deliver)

    write = commands.add_parser("compose", help="现写一封邮件投进收件箱")
    write.add_argument("--from", required=True, help="发件人地址")
    write.add_argument("--subject", required=True, help="主题")
    write.add_argument("--body", required=True, help="正文，\\n 表示换行")
    write.add_argument("--to", default="me@example.com", help="收件人地址")
    write.add_argument("--thread", help="线程 ID；给已有线程就是同线程的另一封邮件")
    write.add_argument("--id", help="邮件 ID；重复使用同一个 ID 用来验证去重")
    write.add_argument("--summary", help="替身给出的摘要；不给就按正文首句生成")
    write.add_argument("--suggestion", help="替身给出的建议")
    write.add_argument("--reply-body", dest="reply_body", help="替身起草回信时用的正文")
    write.set_defaults(run=compose)

    commands.add_parser("clear", help="清空收件箱目录").set_defaults(run=clear)
    return root


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
    if getattr(args, "run", None) is None:
        root.print_help()
        return 2
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
