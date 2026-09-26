# -*- coding: utf-8 -*-
"""
把 VoxType 加入 Windows 开机自启：
在"启动"文件夹生成隐藏运行的 VBS 脚本（启动打包好的 exe，无任何窗口）。
取消自启：删除启动文件夹里的 VoxType.vbs。
"""
import os

exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", "VoxType.exe")
startup = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup")
vbs_path = os.path.join(startup, "VoxType.vbs")

body = 'CreateObject("WScript.Shell").Run """%s""", 0, False\r\n' % exe
with open(vbs_path, "w", encoding="utf-16") as f:
    f.write(body)
print("已添加开机自启：%s" % vbs_path)
print("取消自启：删除该文件即可。")
