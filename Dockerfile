FROM python:3.11-slim

WORKDIR /app

# 复制requirements文件（如果有的话）
COPY requirements.txt .

# 处理 UTF-16 依赖文件并过滤 Windows 专用包，再安装依赖
RUN python -c "from pathlib import Path; p=Path('requirements.txt'); txt=p.read_text(encoding='utf-16'); lines=[ln.strip() for ln in txt.splitlines() if ln.strip() and not ln.lower().startswith(('pywin32==','win32_setctime=='))]; Path('/tmp/requirements.linux.txt').write_text('\n'.join(lines)+'\n', encoding='utf-8')" \
    && pip install --no-cache-dir -r /tmp/requirements.linux.txt -i https://mirrors.huaweicloud.com/repository/pypi/simple/

# 复制项目文件
COPY . .

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["python", "server.py"]
