# Claude Session Migrate

> Move Claude Desktop sessions between accounts — because switching accounts makes them vanish.

**English** · [中文](#中文)

---

## English

### The problem

Claude Desktop keeps each account's Code / Cowork session list in a **separate on-disk directory**
keyed by account UUID. Sign into another account and the list looks empty — your sessions were
never deleted, the app simply isn't looking at that folder anymore.

Worse, there is no export/import for local sessions, and the two obvious workarounds don't work:

- **Copy the app support folder** — you'd have to merge two JSON stores by hand, and one wrong
  UUID lands you in a state where sessions exist on disk but never render.
- **Share one account** — not an option if the accounts belong to different orgs or plans.

### How it works

Claude Desktop splits a session into two parts stored in different places:

```
~/Library/Application Support/Claude/
└── claude-code-sessions/
    ├── <accountA-uuid>/
    │   └── <space-uuid>/
    │       ├── local_<sessionId>.json     ← metadata: what the account's session list reads
    │       ├── deleted_<sessionId>        ← tombstone: hides a session without deleting it
    │       └── archived-sessions.idx      ← archive flags for this space
    └── <accountB-uuid>/ ...

~/.claude/projects/
└── -Users-you-project-foo/
    └── <cliSessionId>.jsonl               ← the actual conversation; NOT account-scoped
```

The key insight: **the transcript is shared, only the ~40 KB metadata blob is per-account.**
So migrating a session is copying one JSON file into the target account's space directory —
the conversation itself never moves, and both accounts can show the same session.

That sounds like `cp`, and it nearly is. What `cp` alone gets wrong:

| Detail | Why it matters |
| --- | --- |
| `deleted_<sessionId>` tombstone in the target | The session stays hidden even though the file is there |
| `archived-sessions.idx` | Archive state doesn't follow the session |
| `spaces-present/<account>/<space>/link-key` | Some builds won't treat the space as available |
| Claude still running | Electron rewrites the session list from memory and clobbers your change |

This tool does all four.

### What you get

**Web UI** (`claude-session-ui`)

- Every local session from **all accounts in one list**, with account and space IDs
- **Grouped by project folder**, collapsible, with per-group select-all
- **Preview before migrating** — initial question plus real conversation turns, with tool-call
  spam and injected `<system-reminder>` blocks stripped out
- One-click migrate for a single row, or bulk migrate for a selection
  (Shift-click range, ⌘A, Space to toggle, ↵ to preview, `/` to search)
- **Duplicate detection** — sessions the target account already has are flagged `Exists`
- **Undo** — the toast after a migration has an Undo button

**CLI** (`claude-session-migrate`) — same engine, scriptable, with `--dry-run`.

### Install

Requirements: **macOS**, **Claude Desktop** (open its Code/Cowork panel at least once),
**Python 3.9+**. No third-party dependencies.

```bash
git clone https://github.com/Heweitao/claude-session-migrate.git
cd claude-session-migrate

# run in place
./claude-session-ui

# or put both commands on your PATH
ln -s "$PWD/claude-session-ui"      ~/.local/bin/
ln -s "$PWD/claude-session-migrate" ~/.local/bin/
```

### Usage

```bash
claude-session-ui                  # local UI + opens your browser
claude-session-ui --port 9000      # pick a port (auto-increments if busy)
claude-session-ui --no-open        # just print the URL
```

**Quit Claude with ⌘Q first.** Closing the window is not enough — the process keeps running and
will overwrite what you write. The UI detects this and refuses to migrate until you confirm.

Then: pick the target account and space in the toolbar → tick the sessions → **Migrate**.
Restart Claude; they appear under the current account.

CLI equivalents:

```bash
claude-session-migrate where                      # resolved account / space / store paths
claude-session-migrate list                       # every session, grouped by account
claude-session-migrate migrate <id> --dry-run     # show the plan, touch nothing
claude-session-migrate migrate <id>               # do it
claude-session-migrate migrate <id> --move        # rename the source to .moved-<ts> afterwards
```

`<id>` accepts `local_<uuid>`, the bare `<uuid>`, or a `cliSessionId` — whatever you have.

### Safety

- **Only metadata is written.** Transcripts are read, never modified.
- **Source account keeps its copy** unless you pass `--move` (which renames to `.moved-<ts>`
  rather than deleting).
- Existing files are backed up to `.bak-<ts>` before being overwritten.
- Every batch is journalled to `~/.claude/session-migrate-journal.json` (last 40), and Undo
  restores backups or withdraws the copied file.
- The server binds **127.0.0.1 only** and makes no outbound network calls.

### Round-trip: continue in B, then go back to A

Because the transcript is shared, this works — with one thing to know.

After you migrate A → B and keep working in B, the conversation itself is appended to the one
shared `.jsonl`, so account A still sees the new turns. What does **not** sync is A's metadata
copy: its turn count, last-activity time and resume pointer stay frozen at migration time.

To refresh them, migrate **B → A** with *Overwrite the N existing copies in the target account*
ticked in the confirmation dialog. You get the same effect from the CLI with `--force`:

```bash
claude-session-migrate migrate <id> --to-account <A-uuid> --force
```

Without that, A's list shows a stale timestamp and may resume from an outdated position.

### Limits

- **Local sessions only** (`local_<id>`). Cloud-side `session_<id>` conversations live on
  Anthropic's servers and cannot be moved locally.
- **macOS only** — the code assumes `~/Library/Application Support/Claude`.
- Claude Desktop is a closed-source Electron app: the on-disk layout above was reverse-engineered
  and may change in a future release. The tool degrades gracefully (it shows what it finds) but
  check `claude-session-migrate where` after major updates.

### License

MIT

---

## 中文

### 背景

Claude 桌面版把每个账号的 Code / Cowork 会话列表，存在**按账号 UUID 分开的目录**里。
换个账号登录，列表就空了——会话没被删，只是应用不再看那个文件夹。

而且本地会话没有导出/导入，两个显而易见的土办法都不好使：

- **直接拷贝应用数据目录** —— 等于手工合并两份 JSON，一个 UUID 不对就变成「磁盘上有、界面里没有」。
- **共用一个账号** —— 账号分属不同组织或套餐时没法这么做。

### 实现原理

一个会话在磁盘上被拆成两半，放在不同地方：

```
~/Library/Application Support/Claude/
└── claude-code-sessions/
    ├── <账号A-UUID>/
    │   └── <空间-UUID>/
    │       ├── local_<会话ID>.json        ← 元数据：账号的会话列表读的就是它
    │       ├── deleted_<会话ID>           ← 墓碑：不删文件也能让会话隐藏
    │       └── archived-sessions.idx      ← 该空间的归档标记
    └── <账号B-UUID>/ ...

~/.claude/projects/
└── -Users-you-project-foo/
    └── <cliSessionId>.jsonl               ← 会话正文；不按账号隔离
```

关键点：**正文是公共的，按账号分开的只有那个约 40 KB 的元数据。**
所以迁移一个会话 = 把一份 JSON 放进目标账号的空间目录，对话本身不动，
迁完之后两个账号都能看到同一个会话。

听起来就是个 `cp`，基本也确实如此。但只用 `cp` 会漏掉四件事：

| 细节 | 漏了会怎样 |
| --- | --- |
| 目标里的 `deleted_<会话ID>` 墓碑 | 文件放对了，会话依然不显示 |
| `archived-sessions.idx` | 归档状态不跟着走 |
| `spaces-present/<账号>/<空间>/link-key` | 部分版本不认为该空间可用 |
| Claude 还在运行 | Electron 用内存里的列表回写，改动被覆盖 |

这四件它都处理了。

### 功能

**网页界面**（`claude-session-ui`）

- **所有账号的本地会话列在一张表里**，带账号与空间标识
- **按项目文件夹分组**，可折叠，支持整组勾选
- **迁移前可预览**——起始问题 + 真实对话轮次，工具调用刷屏和注入的 `<system-reminder>` 都已剥掉
- 单条一键迁移，或多选批量迁移（Shift 连选、⌘A 全选、空格勾选、↵ 预览、`/` 搜索）
- **重复检测**：目标账号已有的会话标 `Exists`，不会产生双份
- **可撤销**：迁移后的提示条上带 Undo 按钮

**命令行**（`claude-session-migrate`）——同一套内核，可脚本化，支持 `--dry-run`。

### 安装

需要：**macOS**、**Claude 桌面版**（至少打开过一次 Code/Cowork 面板）、**Python 3.9+**。无第三方依赖。

```bash
git clone https://github.com/Heweitao/claude-session-migrate.git
cd claude-session-migrate

# 直接跑
./claude-session-ui

# 或者把两个命令装进 PATH
ln -s "$PWD/claude-session-ui"      ~/.local/bin/
ln -s "$PWD/claude-session-migrate" ~/.local/bin/
```

### 使用

```bash
claude-session-ui                  # 起本地服务并打开浏览器
claude-session-ui --port 9000      # 指定端口（被占用会自动 +1）
claude-session-ui --no-open        # 只打印地址
```

**务必先 ⌘Q 完全退出 Claude。** 光关窗口不够——进程还在，会用内存状态覆盖你写入的改动。
界面会检测到并拦下来，要求你确认后才允许强制写入。

然后在工具栏选目标账号和空间 → 勾选会话 → **Migrate**。重启 Claude 即可在当前账号下看到。

命令行等价操作：

```bash
claude-session-migrate where                      # 当前账号 / 空间 / 各类路径
claude-session-migrate list                       # 所有会话，按账号分组
claude-session-migrate migrate <会话ID> --dry-run # 只看计划，不写任何文件
claude-session-migrate migrate <会话ID>           # 执行
claude-session-migrate migrate <会话ID> --move    # 迁完后把源文件改名为 .moved-<ts>
```

`<会话ID>` 支持 `local_<uuid>`、裸 `<uuid>`、或 `cliSessionId`，手上是哪种都行。

### 安全性

- **只写元数据**，正文只读不改。
- **源账号保留副本**，除非显式加 `--move`（是改名成 `.moved-<ts>`，不是删除）。
- 覆盖已有文件前先备份到 `.bak-<ts>`。
- 每批操作记入 `~/.claude/session-migrate-journal.json`（保留最近 40 批），撤销时还原备份或撤出文件。
- 服务只监听 **127.0.0.1**，不发起任何外部网络请求。

### 双向续作：A → B → A

正文共用一份，所以这条路是通的，只有一个点要知道。

A 迁到 B、在 B 里继续聊之后，新内容追加到那唯一的 `.jsonl` 里，A 打开照样看得到。
**不会自动同步的是 A 的元数据副本**：轮数、最近活动时间、续作落点都停在迁移那一刻。

要让它们对齐，就反向迁一次 **B → A**，在确认框里勾上「Overwrite the N existing copies in the
target account」。命令行等价写法：

```bash
claude-session-migrate migrate <会话ID> --to-account <A的UUID> --force
```

不做这一步，A 的列表时间会是旧的，续作落点也可能不对。

### 限制

- **只支持本地会话**（`local_<id>`）。云端 `session_<id>` 会话存在 Anthropic 服务器上，本地搬不了。
- **仅 macOS**，代码假定 `~/Library/Application Support/Claude`。
- Claude 桌面版是闭源 Electron 应用，上面的目录结构是逆向出来的，未来版本可能改。
  工具会降级处理（找到什么显示什么），但大版本更新后建议先跑一次 `claude-session-migrate where` 确认路径仍对得上。

### 许可

MIT

