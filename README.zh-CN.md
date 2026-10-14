# 借脑（agent-peer-bridge）

**把团队里 AI 用得最好的那个人的 AI，借给团队里的每个人。**

让不同电脑上的编程智能体互相派活。一台电脑上的智能体，可以在另一台电脑上**新建、续接、读取、列出、停止真实的会话**：Claude Code 和 Codex 可以互相调用，同类之间也行。中间只隔一个很小的中继。两台机器都不用开放端口，发起方能做的事，也永远不会超过对方机器主人允许的范围。同一套机制也能让整个团队，以及每个同事自己的智能体，用上你手里装备最好的那个智能体，每个人有自己的工作区、权限和规矩。

[English](README.md)

## 为什么做这个

我们的情况是这样：Codex 跑在主力电脑上，Claude 跑在另一台 Mac 上，两边之间靠人来回复制粘贴。这里有两个问题。

第一，人成了传话筒。每次交接都要等人把话搬过去。

第二，一个“接待会话”什么活都接，并不是好的工作单位。它会越聊越长，被反复压缩，慢慢就开始跑偏。更好的做法是每个任务开一个新会话，需要旧上下文的时候再有意识地分叉过来。

所以这个桥不是把聊天消息灌进一个长对话，而是像机器主人自己那样去驱动对方的智能体：用无界面会话，一个任务一个会话，按需续接或分叉，每个会话都有可以回看的完整记录。

## 设计原则

| 原则 | 含义 |
| --- | --- |
| 只出不进 | 执行器主动连中继去领活，不需要入站端口、SSH 或隧道。 |
| 凭据分权 | 调用方令牌只能派活给授权过的机器，机器令牌只能领自己的活。令牌只存哈希，只在创建时显示一次。 |
| 权限由主人定 | 权限模式和沙箱写在执行器的本地配置里，请求改不了。中继会直接拒绝未知参数。 |
| 工作区用名字 | 调用方只能选工作区的名字，名字对应哪个目录只有执行器知道，网络上不传路径。 |
| 每人单独授权 | 每个调用方有自己的工作区、权限和规矩，而且只能看到自己的会话。 |
| 你的会话还是你的 | 你自己开的会话能被列出、能读，但永远不会被写入。想在它的基础上继续，就分叉出一个新会话。 |
| 结果说实话 | 每个写操作都靠 `client_id` 做幂等。领走了却没按时回执的请求，状态标为 `uncertain`（结果未知），绝不偷偷重试。“已受理”永远不会被说成“已完成”。 |

## 快速开始

所有涉及的机器都要安装，需要 Python 3.10 以上，没有第三方依赖：

```bash
git clone <本仓库> && cd agent-peer-bridge && python3 -m pip install -e .
```

**1. 中继**：放在两台机器都能通过 HTTPS 访问到的地方，可以是其中一台机器，也可以是一台云服务器。

```bash
apb-relay --db relay.db add-peer mac-mini                       # 只显示一次机器令牌
apb-relay --db relay.db add-caller codex-main --peers mac-mini  # 只显示一次调用方令牌
apb-relay --db relay.db serve --host 127.0.0.1 --port 8787
```

中继本身只在本机回环地址上提供明文 HTTP。只要流量要离开本机，就先在前面套一层 TLS，可以用 Caddy、Cloudflare Tunnel 或 Tailscale Serve。

**2. 执行器**：装在要接活的机器上。把机器令牌存成 `chmod 600` 的文件，再写一份配置，参考 [`examples/`](examples)，然后运行：

```bash
apb-executor --config ~/.agent-peer-bridge/executor.json
```

用 launchd 或 systemd 让它常驻，示例同样在 [`examples/`](examples) 里。Claude Code 要无人值守运行的话，先用 `claude setup-token` 生成一个长期令牌，再在配置的 `backend_options.oauth_token_file` 里指向它。

**3. MCP 适配器**：装在派活的那台机器上，注册给那边的智能体用，具体写法见英文 README。注册之后，智能体就有了 `list_peers`、`start_session`、`send_message`、`read_session`、`list_sessions`、`stop_session` 和 `request_status` 这几个工具。

**双向**：两台机器都跑一个执行器，再给两边各发一个能调用对方的调用方令牌。

## 把你的智能体共享给团队

团队里那台装备最好的机器上，项目、资料、技能和做事习惯都已经喂进了它的智能体，它可以同时服务所有人。给每个同事（或者同事自己的智能体）发一个调用方令牌，再在执行器配置里写清楚每个人能用什么：

```json
"callers": {
  "anqi":   {"workspaces": ["tools"],
             "rules": "你在帮安琪（运营）做事。可撤回的修改自己改完上线；涉及价格的先停下来说明。",
             "permission": {"permission_mode": "auto"}},
  "bonnie": {"workspaces": ["tools"], "rules_file": "~/.agent-peer-bridge/rules/bonnie.md"},
  "intern": {"workspaces": ["docs"], "permission": {"permission_mode": "plan"}},
  "me":     {"workspaces": "*", "read_owner_sessions": true, "see_all_sessions": true}
}
```

| 配置项 | 作用 |
| --- | --- |
| `workspaces` | 这个人能在哪些工作区开会话，`"*"` 表示全部。 |
| `rules` / `rules_file` | 你给这个人定的规矩，每一轮都会生效：Claude Code 追加到系统提示词里，Codex 放在提示词前面。规矩文件每一轮都会重新读取，改完不用重启。 |
| `permission` | 这个人单独的权限：Claude Code 用 `permission_mode`（比如只提问的人设成 `plan`），Codex 用 `sandbox`（比如 `read-only`）。 |
| `read_owner_sessions` | 能不能列出、读取你自己在这些工作区里的会话，只能读、不能写。你积累的上下文就是这样共享出去的。 |
| `see_all_sessions` | 能不能看到别人通过桥开的会话。默认每个人只能看到自己的。 |

没写进名单的调用方一律拒绝。不写 `callers` 这一节，执行器就是开放模式，所有被中继放行的调用方都能用全部能力，适合一个人自己的两台机器。完整示例见 [`examples/executor.team.json`](examples/executor.team.json)。

## 后端

- **Claude Code**：用 `claude -p --output-format stream-json` 配合 `--session-id`、`--resume`、`--fork-session` 驱动，已经端到端实测。在 macOS 上如果没装命令行版，会自动用 Claude Desktop 内置的那份 Claude Code。它新建的会话就是普通的 Claude Code 会话，可以通过 Claude Desktop 菜单 Help → Troubleshooting → Import Claude Code CLI Sessions… 导入侧边栏。导入之后，这个会话就算你的了：桥这边只读或分叉，不要两边同时续写。
- **Codex**：用 `codex exec --json` 驱动，按 Codex 返回的线程 ID 续接，不支持分叉。已用 codex-cli 0.162.0-alpha.17.2 完成新建、续接、stdin 提示词及 shell 执行的端到端实测。

## 限制

- 中继是单个 SQLite，几台机器够用，不适合大规模集群。
- 不做流式输出，调用方需要轮询 `read_session`。
- 对方机器上任何需要人工批准的动作，都会被直接拒绝，并写进结果里，不会转给调用方批准。审批权始终留在机器主人手里。
- 请求里的文字就是给另一个智能体的提示词。把对方机器当成一个拥有你所配置权限的同事来看待，而不是一个可以随便丢不可信输入的沙箱。

协议细节见 [docs/protocol.md](docs/protocol.md)，安全模型见 [docs/security.md](docs/security.md)。

## 许可

MIT
