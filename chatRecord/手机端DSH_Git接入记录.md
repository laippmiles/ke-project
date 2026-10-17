# 手机端 DSH 接入 GitHub 仓库记录

## 目的

记录在 Android 端 DSH 中接入本仓库的做法，供换设备、重建环境或排查凭据问题时复用。本文只写流程，不保存任何 token 或账号凭据。

## 环境

- Termux / Bionic，git 2.56.0
- 仓库：`https://github.com/laippmiles/ke-project.git`
- 本地路径：`~/.dsh/workspaces/history/ke-project`
- 仓库为 public，匿名即可 clone 和读取

## 步骤

1. 建目录并 clone：

   ```bash
   mkdir -p ~/.dsh/workspaces/history && cd ~/.dsh/workspaces/history
   git clone https://github.com/laippmiles/ke-project.git
   ```

2. 生成 fine-grained PAT（GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens）：

   - Resource owner 选 `laippmiles`
   - Repository access 选 Only select repositories → `ke-project`
   - Repository permissions → **Contents: Read and write**（Metadata 会随之自动标为只读）
   - Expiration 建议 30–90 天，不选 No expiration

3. 写入凭据并启用 store helper：

   ```bash
   umask 077
   printf 'https://laippmiles:%s@github.com\n' "<TOKEN>" > ~/.git-credentials
   chmod 600 ~/.git-credentials
   git config --global credential.helper store
   ```

4. 验证：

   ```bash
   git push --dry-run origin main
   ```

   返回 `Everything up-to-date` 或成功协商，才算写权限可用。

## 已知坑

- **clone 成功不代表有写权限。** 仓库是 public，匿名也能读，所以必须用 `push --dry-run` 单独验证写权限。
- **漏设 Contents 权限**时，pull 一切正常，但 push 报：

  ```
  remote: Permission to laippmiles/ke-project.git denied to laippmiles.
  fatal: ... The requested URL returned error: 403
  ```

  改法：回到 token 设置页，把 Repository permissions → Contents 改成 Read and write，或重新生成 token。
- **Resource owner 选错账号**同样会报 403，即使该账号本身对该仓库有 admin 权限。
- API 返回的 `permissions.push: true` 是**账号**对仓库的权限，和 token 自身的权限无关，不能用来判断 token 能不能推。

## 判断 token 是否可用

- 能读 `https://api.github.com/user` 且返回的 login 与仓库 owner 一致 → token 有效，身份正确。
- 但这一步只证明身份，不证明权限；写权限仍以 `push --dry-run` 为准。
