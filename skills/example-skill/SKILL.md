---
name: example-skill
description: 示例技能，演示 SKILL.md 声明式技能如何被加载、目录注入与 read_skill 读取
description_zh: 示例技能，演示 SKILL.md 声明式技能机制
homepage: https://github.com/dwgx/YuKiKo
user-invocable: true
disable-model-invocation: false
metadata:
  openclaw:
    always: false
---

# 示例技能

这是一个示例技能，验证 SKILL.md 声明式技能的完整链路：

1. `SkillRegistry.load()` 扫描本目录，解析 frontmatter 得到 name/description。
2. `AgentLoop._build_system_prompt` 把目录（name+description）注入 system prompt。
3. 模型命中后调 `read_skill` 工具读取本文件全文，再按步骤执行。

## 用法

当用户询问「示例技能怎么用」或「skill 机制」时：

- 先用 read_skill 读取本文件。
- 再按上面的链路向用户解释：声明式技能 = SKILL.md + 目录注入 + 命中读取。

## 注意事项

- 技能目录不含可执行代码，只做声明式编排（安全边界）。
- 新增技能 = 在 `skills/<name>/SKILL.md` 放一个文件，无需改代码。
