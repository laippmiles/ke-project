#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Drive → LobeHub 资料库 增量同步脚本 v2
=============================================
由 LobeHub 定时任务每小时调用。

环境变量（凭据库 gdrive-sync-sa 自动注入）：
  GDRIVE_SERVICE_ACCOUNT_JSON : 服务账号 JSON 密钥完整内容
  GDRIVE_FOLDER_ID            : 要同步的 Google Drive 文件夹 ID
  SYNC_KB_ID                  : 目标知识库 ID（默认 kb_rGJ6rO2zcC2C）

行为：递归列出文件夹 → 与库内 _sync_state.md 对比增量 → 下载并上传新/变文件
     → 更新 _sync_state.md 和 _sync_log.md。不删除库内已有文件。
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta

KB_ID = os.environ.get("SYNC_KB_ID", "kb_rGJ6rO2zcC2C")
STATE_DOC = "_sync_state.md"
LOG_DOC = "_sync_log.md"
CST = timezone(timedelta(hours=8))


def log(msg):
    print(f"[{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _freshest_jwt():
    """从所有 /tmp/tmp.*/lh wrapper 中挑出 exp 最大的 JWT。"""
    import glob, base64
    best = (0, None)
    for w in glob.glob("/tmp/tmp.*/lh"):
        try:
            s = open(w).read()
        except Exception:
            continue
        m = re.search(r"LOBEHUB_JWT='([^']+)'", s)
        if not m:
            continue
        try:
            pay = m.group(1).split(".")[1]
            pay += "=" * (-len(pay) % 4)
            exp = json.loads(base64.urlsafe_b64decode(pay)).get("exp", 0)
        except Exception:
            continue
        if exp > best[0]:
            best = (exp, m.group(1))
    return best


def lh_env():
    """返回带最新 JWT 的环境变量；无有效凭据返回 None。"""
    exp, tok = _freshest_jwt()
    if not tok or exp - time.time() < 15:
        return None
    env = dict(os.environ)
    env["LOBEHUB_JWT"] = tok
    return env


def run_lh(*args, timeout=90):
    """调用 lh CLI；无有效凭据返回 None。"""
    env = lh_env()
    if env is None:
        return None
    try:
        return subprocess.run(["lh", *args], capture_output=True, text=True,
                              env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def load_credentials():
    sa = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "")
    folder_id = os.environ.get("GDRIVE_FOLDER_ID", "")
    if not sa or not folder_id:
        print("ERROR: 缺少 GDRIVE_SERVICE_ACCOUNT_JSON 或 GDRIVE_FOLDER_ID", file=sys.stderr)
        sys.exit(2)
    return sa, folder_id


def sign_rsa256(private_key_pem, data: bytes) -> bytes:
    """用 openssl 对数据做 RS256 签名（沙盒自带 openssl，免装 python 库）。"""
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as kf:
        kf.write(private_key_pem)
        keypath = kf.name
    try:
        p = subprocess.run(["openssl", "dgst", "-sha256", "-sign", keypath, "-binary"],
                           input=data, capture_output=True, check=True)
        return p.stdout
    finally:
        os.unlink(keypath)


def get_access_token(sa_json_str):
    """服务账号 JWT 换取 access_token。"""
    from urllib.request import urlopen, Request
    from urllib.parse import urlencode
    from urllib.error import HTTPError

    sa = json.loads(sa_json_str)

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    now = int(time.time())
    sign_input = f"{b64({'alg': 'RS256', 'typ': 'JWT'})}.{b64({'iss': sa['client_email'], 'scope': 'https://www.googleapis.com/auth/drive.readonly', 'aud': 'https://oauth2.googleapis.com/token', 'iat': now, 'exp': now + 3600})}".encode()
    sig = sign_rsa256(sa["private_key"], sign_input)
    jwt = sign_input + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")

    data = urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                      "assertion": jwt.decode()}).encode()
    try:
        resp = urlopen(Request("https://oauth2.googleapis.com/token", data=data), timeout=30)
    except HTTPError as e:
        raise RuntimeError(f"获取 access_token 失败: {e.read().decode()}")
    return json.loads(resp.read())["access_token"]


def drive_api(token, url, params):
    from urllib.request import Request, urlopen
    from urllib.parse import urlencode
    full = url + "?" + urlencode(params)
    req = Request(full, headers={"Authorization": f"Bearer {token}"})
    for attempt in range(3):
        try:
            return json.loads(urlopen(req, timeout=60).read())
        except Exception as e:
            if attempt == 2:
                raise
            log(f"API 调用失败，重试 {attempt + 2}/3: {e}")


def list_files(token, folder_id):
    """递归列出文件夹下所有普通文件（跳过 Google 在线文档）。"""
    results = []

    def walk(fid, prefix):
        page_token = None
        q = f"'{fid}' in parents and trashed = false"
        while True:
            params = {"q": q,
                      "fields": "nextPageToken, files(id, name, mimeType, modifiedTime, md5Checksum, size)",
                      "pageSize": 200, "supportsAllDrives": "true",
                      "includeItemsFromAllDrives": "true"}
            if page_token:
                params["pageToken"] = page_token
            resp = drive_api(token, "https://www.googleapis.com/drive/v3/files", params)
            for f in resp.get("files", []):
                if f["mimeType"] == "application/vnd.google-apps.folder":
                    walk(f["id"], f"{prefix}{f['name']}/")
                elif f["mimeType"].startswith("application/vnd.google-apps."):
                    log(f"跳过 Google 在线文档: {prefix}{f['name']}")
                else:
                    f["_path"] = f"{prefix}{f['name']}"
                    results.append(f)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    walk(folder_id, "")
    return results


def download_file(token, file_id, dest):
    from urllib.request import Request, urlopen
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true"
    req = Request(url, headers={"Authorization": f"Bearer {token}"})
    with urlopen(req, timeout=300) as r, open(dest, "wb") as out:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)



# ---------- lh CLI 调用（自愈：绕过 PATH 中过期的 wrapper） ----------

_LH_ENV_CACHE = {}


def _fresh_lh_env():
    import glob as _glob
    import base64 as _b64
    best = None
    for p in _glob.glob("/tmp/tmp.*/lh"):
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if best is None or m > best[0]:
            best = (m, p)
    if not best:
        raise RuntimeError("找不到 lh wrapper")
    src_ = open(best[1], encoding="utf-8").read()
    m = re.search(r"LOBEHUB_JWT='([^']+)'", src_)
    s = re.search(r"LOBEHUB_SERVER='([^']+)'", src_)
    if not m:
        raise RuntimeError("wrapper 中无 JWT")
    jwt = m.group(1)
    payload = jwt.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    exp = json.loads(_b64.urlsafe_b64decode(payload))["exp"]
    remain = exp - int(time.time())
    if remain < 45:
        raise RuntimeError(f"lh JWT 仅剩 {remain}s，等下一次命令重试")
    env = dict(os.environ)
    env["LOBEHUB_JWT"] = jwt
    if s:
        env["LOBEHUB_SERVER"] = s.group(1)
    return env


def lh_run(args, timeout=240):
    env = _fresh_lh_env()
    return subprocess.run(["npx", "-y", "@lobehub/cli", *args],
                          capture_output=True, text=True, env=env, timeout=timeout)


# ---------- 资料库文档读写（lh CLI） ----------

def doc_list():
    import time as _t
    for attempt in range(4):
        r = run_lh("doc", "list", "--json", "-L", "100")
        if r is None:
            log("无有效 lh 凭据，退出（新命令重试）")
            return []
        if r.returncode == 0:
            try:
                return json.loads(r.stdout)
            except json.JSONDecodeError:
                pass
        log(f"doc list 失败(第{attempt+1}次): {(r.stderr or r.stdout or '')[:120]!r}，重试...")
        _t.sleep(2)
    return []


def doc_read_by_title(title):
    docs = [d for d in doc_list() if d.get("title") == title]
    docs.sort(key=lambda d: d.get("createdAt", ""), reverse=True)
    for d in docs:
            r = run_lh("doc", "view", d["id"], "--json")
            if r is not None and r.returncode == 0:
                try:
                    return json.loads(r.stdout).get("content")
                except json.JSONDecodeError:
                    return None
    return None


def doc_delete_by_title(title):
    ids = [d["id"] for d in doc_list() if d.get("title") == title]
    if ids:
        run_lh("doc", "delete", *ids, "--yes")
        return True
    return False
# NOTE: 写入前总是全删同名再建，避免并行运行造成多份状态文件


def doc_create(title, content):
    r = run_lh("kb", "create-doc", KB_ID, "-t", title, "-c", content)
    if r is None or r.returncode != 0:
        log(f"创建文档 {title} 失败: {(r.stderr.strip() if r else 'no auth')}")


def doc_write(title, content):
    doc_delete_by_title(title)
    doc_create(title, content)


def parse_state(content):
    if not content:
        return {}
    m = re.search(r"```(?:json|js|javascript)?\s*(\{.*?\})\s*```", content, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def main():
    dry_run = "--dry-run" in sys.argv
    limit = 0
    for i, a in enumerate(sys.argv):
        if a == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])
    if lh_env() is None:
        log("ERROR: 本命令未注入有效 lh 凭据（JWT 已过期），请在新命令中重试")
        sys.exit(3)
    sa_json, folder_id = load_credentials()
    log("获取 access_token ...")
    token = get_access_token(sa_json)
    log("列出 Drive 文件 ...")
    files = list_files(token, folder_id)
    log(f"Drive 上共有 {len(files)} 个文件")

    state = parse_state(doc_read_by_title(STATE_DOC))
    to_upload = [f for f in files
                 if state.get(f["_path"]) != {"id": f["id"], "mtime": f["modifiedTime"],
                                              "md5": f.get("md5Checksum", "")}]
    if limit and len(to_upload) > limit:
        log(f"本次限制只传 {limit} 个（共待传 {len(to_upload)}，其余下次继续）")
        to_upload = to_upload[:limit]
    if not to_upload:
        log("无新增/变更文件，同步结束。")
        return
    log(f"需上传 {len(to_upload)} 个:")
    for f in to_upload:
        log(f"  - {f['_path']}")
    if dry_run:
        log("[dry-run] 结束，不实际上传。")
        return

    tmpdir = tempfile.mkdtemp(prefix="gdrive_sync_")
    uploaded, failed = [], []
    for f in to_upload:
        dest = os.path.join(tmpdir, f["name"])
        try:
            download_file(token, f["id"], dest)
            r = run_lh("kb", "upload", KB_ID, dest)
            if r is not None and r.returncode == 0:
                uploaded.append(f)
                log(f"已上传: {f['_path']}")
            else:
                err = r.stderr.strip() if r else "no auth (JWT 过期)"
                failed.append((f, err))
                log(f"上传失败: {f['_path']} — {err}")
        except Exception as e:
            failed.append((f, str(e)))
            log(f"下载/上传异常: {f['_path']} — {e}")
        finally:
            if os.path.exists(dest):
                os.unlink(dest)

    for f in uploaded:
        state[f["_path"]] = {"id": f["id"], "mtime": f["modifiedTime"],
                             "md5": f.get("md5Checksum", "")}
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    doc_write(STATE_DOC,
              f"# Google Drive 同步状态\n\n- 来源文件夹 ID: `{folder_id}`\n- 最后同步: {now}\n\n```json\n{json.dumps(state, ensure_ascii=False, indent=2)}\n```\n")
    old_log = doc_read_by_title(LOG_DOC) or "# Google Drive 同步日志\n"
    entry = (f"\n## {now}\n- 总数 {len(files)}；待传 {len(to_upload)}；"
             f"成功 {len(uploaded)}；失败 {len(failed)}\n")
    entry += "".join(f"  - ⚠️ {f['_path']}: {err}\n" for f, err in failed)
    doc_write(LOG_DOC, old_log.rstrip() + "\n" + entry)

    log(f"同步完成: 成功 {len(uploaded)}，失败 {len(failed)}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
