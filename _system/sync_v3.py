#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同步管线 v3：Google Drive → GitHub → LobeHub 知识库
====================================================
由 LobeHub 定时任务每 30 分钟调用（执行 agent 在沙盒运行本脚本）。

流程:
  1. gdrive_toolkit 全量扫描 Drive，与 /root/ke-project 本地副本对比（按 md5）
  2. 有差异 → 下载变更文件 → git commit + push 到 GitHub（版本历史在此沉淀）
  3. 文本类(.md/.txt/.json)变更文件 → lh kb upload 到知识库（媒体不上传）

环境变量: GDRIVE_SERVICE_ACCOUNT_JSON / GDRIVE_FOLDER_ID / GITHUB_ACCESS_TOKEN（凭据库自动注入）
"""

import hashlib
import json
import os
import subprocess
import sys

REPO = "/root/ke-project"
KB_ID = "kb_rGJ6rO2zcC2C"
TEXT_EXT = (".md", ".txt", ".json")


def log(m):
    print(m, flush=True)


def sh(cmd, cwd=None, inp=None):
    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, input=inp)


def local_md5_map():
    m = {}
    for root, _, files in os.walk(os.path.join(REPO, "磕")):
        for f in files:
            p = os.path.join(root, f)
            rel = os.path.relpath(p, REPO)
            m[rel] = hashlib.md5(open(p, "rb").read()).hexdigest()
    return m


def main():
    sys.path.insert(0, "/root")
    os.chdir(REPO)
    import gdrive_toolkit as gt

    token = gt.get_token()
    changed = []

    def walk(fid, prefix):
        for f in gt.list_dir(token, fid):
            name, rel = f["name"], prefix + f["name"]
            if f["mimeType"] == gt.FOLDER_MIME:
                os.makedirs(rel, exist_ok=True)
                walk(f["id"], rel + "/")
            elif f["mimeType"].startswith("application/vnd.google-apps."):
                log(f"跳过在线文档: {rel}")
            else:
                local = os.path.join(REPO, rel)
                need = True
                if os.path.exists(local):
                    need = hashlib.md5(open(local, "rb").read()).hexdigest() != f.get("md5Checksum", "")
                if need:
                    body = gt.call(token, f"{gt.API}/files/{f['id']}",
                                   {"alt": "media", "supportsAllDrives": "true"}, raw=True)
                    os.makedirs(os.path.dirname(local) or REPO, exist_ok=True)
                    open(local, "wb").write(body)
                    changed.append(rel)
                    log(f"变更: {rel} ({len(body)//1024}KB)")

    walk(gt.ROOT, "")

    if not changed:
        log("无变更，本次结束")
        return

    r = sh("git add -A && git commit -m 'Drive 自动同步: %d 个文件变更' && git push" % len(changed), cwd=REPO)
    if r.returncode != 0:
        log("git push 失败: " + (r.stderr or "")[-300:])
        sys.exit(1)
    log("已推送 %d 个变更到 GitHub" % len(changed))

    text_changed = [c for c in changed if c.lower().endswith(TEXT_EXT)]
    ok = fail = 0
    for rel in text_changed:
        r = sh(["lh", "kb", "upload", KB_ID, os.path.join(REPO, rel)])
        if r.returncode == 0:
            ok += 1
        else:
            fail += 1
            log(f"上传失败 {rel}: {(r.stderr or '')[:120]}")
    log(f"知识库更新: 成功 {ok} 失败 {fail}（媒体文件仅存 Git）")


if __name__ == "__main__":
    main()
