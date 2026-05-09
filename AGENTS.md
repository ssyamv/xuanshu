# AGENTS.md instructions for /Users/chenqi/code/xuanshu

## Language

- Always reply with Chinese.

## Image Generation Rule

- When the user asks to generate, regenerate, replace, or visually modify an image/diagram using image generation, use `imagegen`.
- Do not manually patch that image/diagram with local drawing scripts, PIL, SVG rewrites, or ad hoc image edits unless the user explicitly asks for a manual/vector/code-native edit.

## Figma

- 以后读取 Figma 链接时，优先调用本地 Figma MCP；只有本地 MCP 不可用或信息不足时，再考虑其他方式。

## Obsidian Code Bridge

<!-- BEGIN OBSIDIAN-CODE-BRIDGE -->
- Obsidian owns architecture/design/system-explanation notes for this project.
- The repo owns source code, tests, executable configuration, and deployment runbooks.
- Current system overview note: `/Users/chenqi/Obsidian Vault/玄枢/当前系统全景.md`
- Repo path: `/Users/chenqi/code/xuanshu`
- When behavior, architecture, runtime topology, operator commands, or strategy lifecycle changes, update the related Obsidian note in the same work session unless the user explicitly says not to.
<!-- END OBSIDIAN-CODE-BRIDGE -->
