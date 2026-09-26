# VoxType

VoxType 是一个面向 Windows 的轻量级全局语音输入工具。按一下 `F10` 开始录音，再按一下结束，识别文字会自动填入当前输入框。

## 功能

- `F10` 全局开始/结束录音
- 识别结果自动粘贴到当前焦点输入框
- 默认只粘贴，不自动发送
- 黑色极简悬浮窗和实时音量反馈
- 支持自定义强调色
- API Key 仅保存在本机，不写入源码、构建产物或日志

语音识别默认使用火山引擎豆包流式语音识别 2.0，费用由使用者的账号承担。

## 使用

1. 从 GitHub Releases 下载 `VoxType.exe`。
2. 双击运行并填写 API Key、Resource ID 和 WebSocket URL。
3. 按一下 `F10` 开始录音，再按一下结束。
4. 右键悬浮窗可选择强调色、重新配置 API 或退出。

配置和日志位置：

```text
%APPDATA%\VoxType\config.json
%APPDATA%\VoxType\voice_input.log
```

## 开发

需要 Windows 10/11 和 Python 3.12 或更高版本。

```powershell
python -m pip install -r requirements.txt
python voice_input.py
```

重新配置 API：

```powershell
python voice_input.py --setup
```

## 构建

```powershell
python -m PyInstaller --clean --noconfirm VoxType.spec
```

构建产物：

```text
dist\VoxType.exe
```

## 发布

推送版本标签后，GitHub Actions 会构建并发布 EXE：

```powershell
git tag v1.0.2
git push origin v1.0.2
```

## 隐私与安全

- API Key 不会提交到 Git。
- 日志不记录识别文本。
- 音频会发送到用户配置的语音识别服务。
- 不要提交个人 `config.json`、日志或本地构建目录。

## 许可证

MIT License，详见 [LICENSE](LICENSE)。
