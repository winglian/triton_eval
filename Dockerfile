FROM axolotlai/axolotl:main-latest

RUN pip install weave httpx

COPY ./axolotl_dev /app/axolotl_dev
WORKDIR /app/axolotl_dev



# Build with: docker build -t ghcr.io/tcapelle/triton_eval:latest .
# Push with: docker push ghcr.io/tcapelle/triton_eval:latest
