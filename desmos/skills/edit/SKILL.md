---
name: edit
description: Replace exactly one unique string in an existing file. Use for targeted edits instead of rewriting the whole file.
---

# Edit

Prefer the frozen tag:

```
<edit path="pkg/file.py">
old unique snippet
---
new snippet
</edit>
```

Or from Python in the kernel:

```python
edit.run("pkg/file.py", old_str, new_str)
```

`old` must occur exactly once. If it matches more than once, widen the snippet.
