FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Public roots + the Zscaler root CA, so outbound HTTPS works through ZIA SSL inspection.
# azure/02-mcp-server.sh copies your Zscaler root CA into the build context as zscaler-root-ca.crt.
COPY zscaler-root-ca.crt /tmp/zscaler-root-ca.crt
RUN cat "$(python -c 'import certifi; print(certifi.where())')" /tmp/zscaler-root-ca.crt > /etc/ssl/mcp-ca-bundle.pem
ENV SSL_CERT_FILE=/etc/ssl/mcp-ca-bundle.pem PYTHONUNBUFFERED=1
COPY server.py .
EXPOSE 8080
USER 65534
CMD ["python", "server.py"]
