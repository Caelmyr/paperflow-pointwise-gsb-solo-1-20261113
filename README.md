# PaperFlow 文档图像数字化工作台

原生HTML/CSS/JavaScript + Flask + OpenCV + 本地Tesseract中英文识别，JSON原子持久化。

Docker启动：`docker compose up -d --build`，打开 http://127.0.0.1:8888 。首次构建需下载依赖，之后核心功能离线可用。端口占用时用环境变量PAPERFLOW_PORT指定其他宿主端口。PAPERFLOW_DATA_DIR可指定宿主机独立数据目录，默认./data；不要让多个服务写同一工作区。停止：`docker compose down`。

本机启动：`bash start.sh --port 8888`。Windows用start.bat或start.bat 8889，stop.bat停止；脚本未在Windows实机验证。固定版本依赖见requirements.lock.txt。

首次打开自带中英文演示页及手机拍摄练习图。支持图片/PDF导入、校正、光照、色彩、区域提取、OCR、人工校订、多页编排、导出。页面书签与页面复核直接连接已有后端服务；导出归档可选择完整或不携带原件。更多工具包括工作流、快照、质量诊断及备份。参数及接口见docs/API.md。

印刷网纹治理（工作台「网纹治理」面板，演示文档「印刷网纹 · 分区抑制练习」）：对彩色杂志/复印插图的规则网点与扫描摩尔纹，先在原始分辨率按图块做频谱检测，给出高置信/待确认/依据不足三级结论与中文依据，再由人按区域确认抑制或保留；文字、细线与本就重复的织物纹理可手动保护且逐位不变。全分辨率预览可放大到 100% 核对波纹能量下降与细节损失，保存的整图和 1:1 局部图与确认预览逐位一致；重复调整会跳过已抑制区域，原图与历史版本随时可回看。

原稿、处理图与来源分别留存。demo/practice中的图片、PDF和文本可直接导入练习。字体许可证见resources/FONT-LICENSE.txt。已有项目测试：`python -m pytest tests/test_acceptance.py -q`；普通测试不代替对复杂业务边界的核验。
