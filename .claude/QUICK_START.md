# Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt
pytest                              # all (fast)
pytest tests/test_processor.py -x   # one file
pytest -k webhook -x                # by keyword
docker buildx bake --load           # build image
kustomize build k8s/overlays/prod | kubectl apply -f -
kubectl -n comment-commander logs -f deploy/comment-commander
```

Local server: `uvicorn main:create_app --factory --reload` from `src/`.
