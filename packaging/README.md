# Windows 可运行程序构建

第一版发布目标是 Windows x64 `onedir` 压缩包，而不是安装程序。构建机必须是
Windows，因为 PyInstaller 不能在 Linux/WSL 直接生成可执行的 Windows 二进制。

在 Windows PowerShell 中，从仓库根目录运行：

```powershell
.\packaging\build-windows.ps1
```

构建结果位于 `dist\AgentDataPipeline\`。将该目录压缩为：

`AgentDataPipeline-windows-x64-v1.3.0.zip`

用户解压后双击 `AgentDataPipeline.exe` 即可。运行时数据不会写回程序目录，而会
写入 `%APPDATA%\AgentDataPipeline\`。

目录中的 `AgentDataPipelineWorker.exe` 是主程序内部调用的流水线阶段执行器，负责
把阶段日志回传到 Web 控制台，用户无需手动运行。