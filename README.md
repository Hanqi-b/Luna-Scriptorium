# Luna Scriptorium

这是一个正在验证中的 Codex 整本书翻译 Skill。当前输入是完整的 UTF-8 Markdown（或 TXT），输出是中文 Markdown；EPUB/PDF 需要先整理为 Markdown，暂不生成 EPUB。

翻译分两步准备：先检查原文 Markdown，再自动规划并锁定章节和分块。运行时由主 agent 调度四个 GPT-6 Luna Max 翻译 agent，记录进度以便中断后恢复。Skill 名称是 `$luna-scriptorium`。使用方法和运行约束见 [`luna-scriptorium/SKILL.md`](luna-scriptorium/SKILL.md)；验证范围及限制见 [`VALIDATION.md`](VALIDATION.md)。

## 让 Codex 发现 Skill

在此仓库根目录执行：

```bash
mkdir -p "$HOME/.agents/skills"
ln -s "$(pwd)/luna-scriptorium" "$HOME/.agents/skills/luna-scriptorium"
```

Codex 可自动发现新增 Skill；如果没有显示，重启 Codex。翻译运行还需要在当前受信任项目中启用并验证 `.codex/hooks.json` 的生命周期 hooks。仓库中的 `.artifacts/` 是本地实验记录，不会上传到 GitHub。

## 验证

```bash
python3 -m unittest discover -s tests -p 'test_runner.py' -q
```
