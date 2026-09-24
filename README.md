# Luna Scriptorium

`$luna-scriptorium 翻译 <book>` 面向整本书。主 agent 自行识别并准备用户给的文件或文件夹，将可读取的正文整理为锁定的 Markdown/TXT 工作单元，再协调固定的四个 GPT-6 Luna Max worker 完成初译、章节审校、全书一致性检查，最后输出中文 Markdown。合理可用的提取和 OCR 方法都失败时，才报告具体阻塞。

[Skill 使用说明](luna-scriptorium/SKILL.md) · [验证范围](VALIDATION.md)

## 本地发现

此仓库的 `luna-scriptorium/` 是 Skill 目录。可用软链接将其加入个人发现目录：

```bash
mkdir -p "$HOME/.agents/skills"
ln -s "$(pwd)/luna-scriptorium" "$HOME/.agents/skills/luna-scriptorium"
```

## 测试

```bash
python3 -m unittest discover -s tests -p 'test_runner.py' -q
```

本仓库只保存工作单元状态和输出；模型调用、实际格式提取和 host worker 生命周期由 Codex root agent 执行。
