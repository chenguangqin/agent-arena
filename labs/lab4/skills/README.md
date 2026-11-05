在这个目录下为每份政策建一个 skill：`skills/<skill 名字>/SKILL.md`。

SKILL.md 以 YAML frontmatter 开头：

```markdown
---
name: refund-policy
description: 什么时候应该加载这个 skill（写清楚触发条件）
---
# 正文：按步骤写清楚怎么做
```

可选的资源文件放在 `references/`、`scripts/` 或 `assets/` 子目录中，加载 skill 时会列出这些文件。
