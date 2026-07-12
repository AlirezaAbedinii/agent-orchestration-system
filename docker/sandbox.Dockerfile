# Minimal sandbox image for the code_exec tool. Runs snippets as a non-root
# user; the invoking command adds --network none and CPU/memory/pids limits.
FROM python:3.12-slim

RUN useradd --create-home --shell /usr/sbin/nologin sandbox
USER sandbox
WORKDIR /home/sandbox

CMD ["python", "-I", "-"]
