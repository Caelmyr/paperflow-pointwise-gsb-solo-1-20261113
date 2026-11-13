# PaperFlow 文档图像数字化工作台

原生HTML/CSS/JavaScript + Flask + OpenCV + 本地Tesseract中英文识别，JSON原子持久化。

Docker启动：`docker compose up -d --build`，打开 http://127.0.0.1:8888 。首次构建需下载依赖，之后核心功能离线可用。端口占用时用环境变量PAPERFLOW_PORT指定其他宿主端口。PAPERFLOW_DATA_DIR可指定宿主机独立数据目录，默认./data；不要让多个服务写同一工作区。停止：`docker compose down`。

本机启动：`bash start.sh --port 8888`。Windows用start.bat或start.bat 8889，stop.bat停止；脚本未在Windows实机验证。固定版本依赖见requirements.lock.txt。

首次打开自带中英文演示页及手机拍摄练习图。支持图片/PDF导入、校正、光照、色彩、区域提取、OCR、人工校订、多页编排、导出。页面书签与页面复核直接连接已有后端服务；导出归档可选择完整或不携带原件。更多工具包括工作流、快照、质量诊断及备份。参数及接口见docs/API.md。

原稿、处理图与来源分别留存。demo/practice中的图片、PDF和文本可直接导入练习。字体许可证见resources/FONT-LICENSE.txt。已有项目测试：`python -m pytest tests/test_acceptance.py -q`；普通测试不代替对复杂业务边界的核验。

## 物理尺寸依据

页面像素本身不能决定纸张大小。每页保存独立的「物理尺寸依据」（page.measurement）：导入时只采信来源文件自带的可信密度（图片 DPI）或 PDF 页面点尺寸，缺少可信密度的照片保持「无依据」状态，绝不暗中套用统一密度。可在「物理尺寸依据」面板按已知纸张大小或一条纸边的实测长度逐页人工确认。

旋转、镜像、裁切、重新采样、透视校正和自动纠偏对依据的影响各不相同：旋转仅交换宽高，重采样保持物理尺寸并反推新密度，裁切按像素缩小物理尺寸，透视/纠偏/加白边会使原依据失效并交回用户确认。导出 PDF 时纸幅只取每页已确认的物理尺寸，导出框里的 DPI 仅改变嵌入图像的清晰度，不改变纸张大小；缺少依据的页面会拒绝导出 PDF 并列出页名。混合尺寸文档逐页使用各自依据，文字层坐标按当前图像像素映射到纸面点，恢复或复制历史版本只采用该版本自身保存的依据。相关接口见 docs/API.md，专项测试：`python -m pytest tests/test_physical_size.py -q`。
