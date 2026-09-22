"""真实抓取结果校验脚本（tester 用，不在 echolens/ 内）。

用法：.venv/Scripts/python.exe acceptance_validate.py <bvid>
读取 data/comments/<bvid>.jsonl 与 .meta.json，校验：
- 每行合法 JSON
- 字段恰为 8 个白名单
- 无身份字段（mid/uname/avatar/sign/level_info/member 等）
- rpid 无重复
- 打印 meta.json 的 stats 汇总
"""
import json
import sys

WHITELIST = {"rpid", "root", "parent", "like", "ctime", "message", "rcount", "is_sub"}
IDENTITY = {"mid", "uname", "avatar", "sign", "level_info", "member", "vip", "fans_detail", "rank"}


def main():
    bvid = sys.argv[1]
    base = f"data/comments/{bvid}"
    jsonl = f"{base}.jsonl"
    meta_path = f"{base}.meta.json"

    rows = []
    bad_json = 0
    field_bad = 0
    identity_hits = 0
    with open(jsonl, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                bad_json += 1
                continue
            rows.append(r)
            if set(r.keys()) != WHITELIST:
                field_bad += 1
            for k in IDENTITY:
                if k in r:
                    identity_hits += 1

    rpids = [r["rpid"] for r in rows]
    dups = len(rpids) - len(set(rpids))
    roots = [r for r in rows if not r["is_sub"]]
    subs = [r for r in rows if r["is_sub"]]

    print(f"=== {bvid} 校验 ===")
    print(f"JSON 坏行: {bad_json}")
    print(f"字段不符白名单行数: {field_bad}")
    print(f"身份字段命中行数: {identity_hits}")
    print(f"总行数: {len(rows)}  唯一 rpid: {len(set(rpids))}  重复 rpid: {dups}")
    print(f"根评论: {len(roots)}  子评论: {len(subs)}")

    try:
        meta = json.load(open(meta_path, encoding="utf-8"))
        print("meta.stats:", json.dumps(meta.get("stats"), ensure_ascii=False))
        print("reply_count_api:", meta.get("stats", {}).get("reply_count_api"))
        total = meta.get("stats", {}).get("total_stored")
        api = meta.get("stats", {}).get("reply_count_api")
        if api:
            print(f"损耗比: {(total - api) / api * 100:.2f}%  (总{total} / 报告{api})")
    except OSError:
        print("meta.json 不存在")


if __name__ == "__main__":
    main()
