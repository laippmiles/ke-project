#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Google Drive 直连工具包（服务账号 · 读写权限）

命令:
  list [路径]                    列出该目录内容（默认根目录）
  tree [路径]                    递归列出目录树
  read <路径> [另存为]           读取文件（打印内容或另存到本地）
  write <本地文件> <Drive路径>   上传新文件，同名旧文件会被覆盖
  trash <路径>                   移入回收站（尽力而为，视权限而定）
  meta <路径>                    查看文件元数据

路径以共享的根目录为基准，例: 磕/00_入口与规章/磕_GoogleDrive_入口索引.md
"""
import base64, json, os, subprocess, sys, tempfile, time
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError

SCOPE = "https://www.googleapis.com/auth/drive"  # 读写权限（用户已共享编辑者权限）
ROOT = os.environ.get("GDRIVE_FOLDER_ID", "")
API = "https://www.googleapis.com/drive/v3"
UP = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"


def b64(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def get_token():
    sa = json.loads(os.environ["GDRIVE_SERVICE_ACCOUNT_JSON"])
    now = int(time.time())
    sign_input = f"{b64({'alg': 'RS256', 'typ': 'JWT'})}.{b64({'iss': sa['client_email'], 'scope': SCOPE, 'aud': 'https://oauth2.googleapis.com/token', 'iat': now, 'exp': now + 3600})}".encode()
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as kf:
        kf.write(sa["private_key"])
        kp = kf.name
    try:
        sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", kp, "-binary"],
                             input=sign_input, capture_output=True, check=True).stdout
    finally:
        os.unlink(kp)
    jwt = sign_input + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")
    data = urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                      "assertion": jwt.decode()}).encode()
    r = json.loads(urlopen(Request("https://oauth2.googleapis.com/token", data=data),
                           timeout=30).read())
    return r["access_token"]


def call(token, url, params=None, method="GET", data=None, headers=None, raw=False):
    full = url + ("?" + urlencode(params) if params else "")
    h = {"Authorization": f"Bearer {token}"}
    h.update(headers or {})
    req = Request(full, data=data, headers=h, method=method)
    try:
        resp = urlopen(req, timeout=120)
        body = resp.read()
        return body if raw else (json.loads(body) if body else {})
    except HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")


def _esc(name):
    return name.replace("\\", "\\\\").replace("'", "\\'")


def resolve(token, path):
    """把相对路径解析为 file id；空路径返回根目录 id。"""
    cur = ROOT
    for part in [p for p in path.strip("/").split("/") if p]:
        q = f"'{cur}' in parents and name = '{_esc(part)}' and trashed = false"
        r = call(token, f"{API}/files", {"q": q, "fields": "files(id,name,mimeType)",
                                         "supportsAllDrives": "true",
                                         "includeItemsFromAllDrives": "true"})
        hits = r.get("files", [])
        if not hits:
            raise FileNotFoundError(f"找不到: {path}（缺「{part}」）")
        cur = hits[0]["id"]
    return cur


def parent_of(path):
    parts = [p for p in path.strip("/").split("/") if p]
    return "/".join(parts[:-1]), parts[-1]


def fmt(f):
    mark = "/" if f["mimeType"] == FOLDER_MIME else ""
    size = f' ({int(f["size"]) // 1024}KB)' if f.get("size") else ""
    return f"{f['name']}{mark}{size}"


def list_dir(token, folder_id):
    q = f"'{folder_id}' in parents and trashed = false"
    return call(token, f"{API}/files", {
        "q": q, "pageSize": "200", "orderBy": "folder,name",
        "fields": "nextPageToken, files(id,name,mimeType,size,modifiedTime)",
        "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}).get("files", [])


def tree(token, fid, prefix="", out=None):
    out = [] if out is None else out
    for f in list_dir(token, fid):
        out.append(fmt(f).replace(f["name"], prefix + f["name"], 1))
        if f["mimeType"] == FOLDER_MIME:
            tree(token, f["id"], f"{prefix}{f['name']}/", out)
    return out


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    token = get_token()
    if cmd == "list":
        path = sys.argv[2] if len(sys.argv) > 2 else ""
        for f in list_dir(token, resolve(token, path)):
            print(fmt(f))
    elif cmd == "tree":
        path = sys.argv[2] if len(sys.argv) > 2 else ""
        for line in tree(token, resolve(token, path)):
            print(line)
    elif cmd == "meta":
        f = call(token, f"{API}/files/{resolve(token, sys.argv[2])}",
                 {"fields": "id,name,mimeType,size,modifiedTime,md5Checksum",
                  "supportsAllDrives": "true"})
        print(json.dumps(f, ensure_ascii=False, indent=2))
    elif cmd == "read":
        fid = resolve(token, sys.argv[2])
        body = call(token, f"{API}/files/{fid}",
                    {"alt": "media", "supportsAllDrives": "true"}, raw=True)
        if len(sys.argv) > 3:
            open(sys.argv[3], "wb").write(body)
            print(f"已下载 {len(body)} 字节 → {sys.argv[3]}")
        else:
            sys.stdout.buffer.write(body)
    elif cmd == "write":
        local, drive_path = sys.argv[2], sys.argv[3]
        parent_path, name = parent_of(drive_path)
        parent_id = resolve(token, parent_path) if parent_path else ROOT
        content = open(local, "rb").read()
        ext = local.rsplit(".", 1)[-1].lower()
        mime = {"md": "text/markdown", "txt": "text/plain",
                "json": "application/json", "html": "text/html"}.get(ext,
                                                                     "application/octet-stream")
        q = f"'{parent_id}' in parents and name = '{_esc(name)}' and trashed = false"
        hits = call(token, f"{API}/files", {"q": q, "fields": "files(id)",
                                            "supportsAllDrives": "true"}).get("files", [])
        if hits:
            call(token, f"{UP}/files/{hits[0]['id']}",
                 {"uploadType": "media", "supportsAllDrives": "true"},
                 method="PATCH", data=content, headers={"Content-Type": mime})
            print(f"已覆盖: {drive_path} ({len(content)} 字节)")
        else:
            meta = {"name": name, "parents": [parent_id]}
            b = "._lobe_boundary_"
            body = (f"--{b}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                    f"{json.dumps(meta)}\r\n--{b}\r\nContent-Type: {mime}\r\n\r\n"
                    ).encode() + content + f"\r\n--{b}--".encode()
            r = call(token, f"{UP}/files", {"uploadType": "multipart",
                                            "supportsAllDrives": "true",
                                            "fields": "id,name"},
                     method="POST", data=body,
                     headers={"Content-Type": f"multipart/related; boundary={b}"})
            print(f"已上传: {drive_path} ({len(content)} 字节, id={r.get('id')})")
    elif cmd == "trash":
        fid = resolve(token, sys.argv[2])
        call(token, f"{API}/files/{fid}", {"supportsAllDrives": "true"}, method="PATCH",
             data=json.dumps({"trashed": True}).encode(),
             headers={"Content-Type": "application/json"})
        print(f"已移入回收站: {sys.argv[2]}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
