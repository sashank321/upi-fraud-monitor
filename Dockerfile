FROM python:3.12-slim
WORKDIR /app
COPY UI-LICENSES.md ./
COPY app.py autonomous_store.py demo_auth.py hosted_agent.py investigation.py laya_decisions.py openbot_bridge.py orchestration.py payments.py specialists.py ./
COPY web/index.html web/ui.js web/ui.css web/
COPY web/react-bits/LICENSE.md ./REACT-BITS-LICENSE.md
RUN mkdir /app/data
ENTRYPOINT ["python", "-u", "/app/app.py"]
