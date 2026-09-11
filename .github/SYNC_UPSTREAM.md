# 依赖备份同步约定

`sync` 保存工作流与测试，`master` 是 `userProg` 的 CherryUSB 子模块来源。
`master` 上已发布的提交必须永久保留在该分支的祖先链中；只保存在另一条归档分支不满足此约束。

每次同步必须满足：

- 旧 `master` 是新 `master` 的祖先，已引用的提交 SHA 及对应文件内容不变。
- 新 `master` 的文件树与本次获取的上游 `master` 一致。
- 正常更新直接快进；上游分叉、回退或重建历史时，新增合并提交，以旧 `master` 为第一父提交、上游提交为第二父提交，文件树采用上游版本。
- 上游提交已在历史中且文件树相同时，不重复产生提交。
- 不重置、变基、强推或删除远端分支，不删除或覆盖已有标签。
- 发布前检查祖先关系；通过普通的原子推送同时更新 `master` 和标签。获取失败、并发冲突、标签冲突或服务端拒绝时停止同步。

工作流先运行回归测试，通过后才允许同步。测试直接执行工作流中的 Bash，使用本地 Git 仓库验证同步、失败处理、垃圾回收后仅克隆 `master` 及固定 SHA 的子模块恢复：

```sh
python3 -m pip install PyYAML==6.0.3
python3 -B -m unittest discover -s .github/tests -v
```

子模块的版本由主项目中的 gitlink SHA 固定。`.gitmodules` 的 `branch = master` 用于 `git submodule update --remote`；普通的 `git submodule update --init --recursive` 按固定 SHA 检出。
更新上游不会改变已有 pin 的源码；主动更新 pin 后的源码兼容性需要在消费项目中验证。

上述约束保证本工作流不会使既有 `master` 提交失去引用。服务端还需保护 `master`，禁止删除与非快进更新，并将此规则应用于管理员；允许合并提交，不要求线性历史。此文件不自动配置 GitHub 分支保护。
仓库被删除、访问权限被撤销或服务不可用，不在 CI 脚本能够保证的范围内。
