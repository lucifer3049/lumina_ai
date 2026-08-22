"""請求層的 middleware（02 §api）。

`api/main.py` 原本兩者都收著，並留話「拆檔留給第一個真的需要第二條 middleware 的
工作包」——那就是 2A-4（稽核）。順序約束由
`tests/unit/test_audit_registry.py::test_audit_middleware_runs_inside_the_request_context`
釘住，不是只寫在註解裡。
"""
