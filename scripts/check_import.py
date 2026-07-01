import sys, os
sys.path.insert(0, os.path.dirname(__file__))
print("sys.path:", sys.path)
try:
    import worker_model_config
    print("OK - imported as worker_model_config")
    print("Has should_fallback:", hasattr(worker_model_config, 'should_fallback'))
except Exception as e:
    print(f"FAIL worker_model_config: {e}")
try:
    from worker_model_config import should_fallback
    print("OK - direct from import")
except Exception as e:
    print(f"FAIL direct import: {e}")
