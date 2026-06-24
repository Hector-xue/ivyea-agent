"""代码 / 工程类 CLI 子命令（从 cli.py 拆出，降低 god-file 体量）。

处理函数接收 argparse 的 args、返回退出码，由 cli.build_parser 通过
set_defaults(func=...) 绑定。模块级只依赖 argparse/json/sys/config，其余在
函数内惰性 import，避免循环依赖。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config


def _cmd_workspace(args: argparse.Namespace) -> int:
    from . import workspace
    root = args.root or "."
    options = workspace.ScanOptions(
        max_files=args.max_files,
        max_bytes=args.max_bytes,
        include_hidden=args.include_hidden,
    )
    if args.action == "index":
        idx = workspace.build_index(root, options)
        path = workspace.save_index(idx)
        print(workspace.render_index(idx, path))
        return 0
    if args.action == "search":
        rows = workspace.search(args.query or "", root=root, limit=args.limit)
        print(workspace.render_search(rows, args.query or ""))
        return 0 if rows else 1
    if args.action == "map":
        idx = workspace.load_index(root)
        if not idx or args.refresh:
            idx = workspace.build_index(root, options)
            workspace.save_index(idx)
        print(workspace.render_map(workspace.project_map(root)))
        return 0
    if args.action == "graph":
        idx = workspace.load_index(root)
        if not idx or args.refresh:
            idx = workspace.build_index(root, options)
            workspace.save_index(idx)
        print(workspace.render_graph(workspace.dependency_graph(root, limit=args.limit)))
        return 0
    if args.action == "inspect":
        idx = workspace.load_index(root)
        if not idx or args.refresh:
            idx = workspace.build_index(root, options)
            workspace.save_index(idx)
        print(workspace.render_inspect(workspace.project_inspect(root)))
        return 0
    if args.action == "symbols":
        idx = workspace.load_index(root)
        if not idx or args.refresh:
            idx = workspace.build_index(root, options)
            workspace.save_index(idx)
        print(workspace.render_symbols(workspace.symbol_index(root, query=args.query or "", limit=args.limit)))
        return 0
    if args.action == "impact":
        idx = workspace.load_index(root)
        if not idx or args.refresh:
            idx = workspace.build_index(root, options)
            workspace.save_index(idx)
        print(workspace.render_impact(workspace.impact_analysis(args.target or args.query or "", root=root, limit=args.limit)))
        return 0
    if args.action == "explain":
        idx = workspace.load_index(root)
        if not idx or args.refresh:
            idx = workspace.build_index(root, options)
            workspace.save_index(idx)
        print(workspace.render_explain(workspace.explain(args.target or args.query or ".", root=root)))
        return 0
    return 2


def _cmd_task(args: argparse.Namespace) -> int:
    from . import task_runner
    try:
        if args.action == "create":
            steps = []
            if args.step:
                steps.extend(args.step)
            elif args.steps:
                steps.extend([s.strip() for s in args.steps.split("|") if s.strip()])
            task = task_runner.create(args.title or "", steps=steps, notes=args.notes or "", workspace=args.workspace or "")
            print(task_runner.render(task))
            return 0
        if args.action == "list":
            print(task_runner.render_list(task_runner.list_tasks(limit=args.limit, status=args.status or "")))
            return 0
        if args.action == "show":
            print(task_runner.render(task_runner.load(args.id)))
            return 0
        if args.action == "start":
            print(task_runner.render(task_runner.start_next(args.id, note=args.notes or "")))
            return 0
        if args.action == "step":
            print(task_runner.render(task_runner.update_step(args.id, args.index, args.status, note=args.notes or "")))
            return 0
        if args.action == "status":
            print(task_runner.render(task_runner.set_status(args.id, args.status, note=args.notes or "")))
            return 0
        if args.action == "log":
            print(task_runner.render(task_runner.append_log(args.id, args.notes or "")))
            return 0
        if args.action == "resume":
            print(task_runner.render_resume(task_runner.load(args.id)))
            return 0
        if args.action == "continue":
            from . import service
            result = service.task_continue(args.id, {
                "message": args.message or args.notes or "",
                "max_steps": args.max_steps,
                "plan_mode": not bool(args.execute),
                "inject_retrieval": False,
            })
            if not result.get("ok"):
                chat_error = result.get("chat", {}) if isinstance(result.get("chat"), dict) else {}
                print(
                    result.get("detail")
                    or result.get("error")
                    or chat_error.get("detail")
                    or chat_error.get("error")
                    or "task continue 失败",
                    file=sys.stderr,
                )
                return 1
            chat = result.get("chat") or {}
            if chat.get("text"):
                print(chat["text"])
            print()
            print(task_runner.render(result["task"]))
            return 0
    except Exception as e:  # noqa: BLE001
        print(f"task 失败：{e}", file=sys.stderr)
        return 1
    return 2


def _cmd_gitops(args: argparse.Namespace) -> int:
    from . import git_workflow, permission
    root = args.root or "."
    if args.action == "status":
        print(git_workflow.render_status(git_workflow.status(root)))
        return 0
    if args.action == "diff":
        print(git_workflow.render_diff(git_workflow.diff_summary(root, staged=args.staged)))
        return 0
    if args.action == "workflows":
        print(git_workflow.render_workflows(git_workflow.workflows(root)))
        return 0
    if args.action == "ci":
        result = git_workflow.ci_status(root, remote=args.remote, limit=args.limit, timeout=args.timeout)
        print(git_workflow.render_ci_status(result))
        return 0 if result.get("ok") else 1
    if args.action == "release-plan":
        plan = git_workflow.release_plan(args.version or "", root)
        print(git_workflow.render_release_plan(plan))
        return 0 if plan.get("ok") else 1
    if args.action in ("stage", "commit", "tag"):
        result = git_workflow.write_action(
            args.action,
            root=root,
            files=args.file or [],
            message=args.message or "",
            tag=args.tag or "",
            execute=False,
            timeout=args.timeout,
        )
        preview = git_workflow.render_write_action(result)
        if not result.get("ok"):
            print(preview)
            return 1
        if args.execute:
            if not args.yes:
                state = permission.PermissionState()
                intent = {"op_type": f"git.{args.action}"}
                decision = permission.request_intent(intent, preview, state)
                if decision != permission.APPROVE:
                    print("已取消。")
                    return 1
            result = git_workflow.write_action(
                args.action,
                root=root,
                files=args.file or [],
                message=args.message or "",
                tag=args.tag or "",
                execute=True,
                timeout=args.timeout,
            )
        print(git_workflow.render_write_action(result))
        return 0 if result.get("ok") else 1
    return 2


def _cmd_codereview(args: argparse.Namespace) -> int:
    from . import code_review
    result = code_review.review_diff(args.root or ".", staged=args.staged)
    print(code_review.render(result))
    if not result.get("ok"):
        return 1
    return 1 if any(f.get("severity") == "high" for f in result.get("findings", [])) else 0


def _cmd_patch(args: argparse.Namespace) -> int:
    from . import patcher, permission

    try:
        if args.action == "make":
            spec = patcher.make_spec(args.path or "", args.old or "", args.new or "")
            text = json.dumps(spec, ensure_ascii=False, indent=2)
            if args.output:
                out = Path(args.output).expanduser()
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(text + "\n", encoding="utf-8")
                print(f"已写入 patch spec：{out}")
            else:
                print(text)
            return 0
        if args.action in ("validate", "apply"):
            spec = patcher.load_spec(args.spec)
            if args.action == "validate":
                result = patcher.validate_spec(spec, root=args.root or ".")
                print(patcher.render_validation(result))
                return 0 if result["ok"] else 1
            validation = patcher.validate_spec(spec, root=args.root or ".")
            if not validation["ok"]:
                print(patcher.render_apply({"ok": False, "applied": False, "validation": validation, "message": "patch 校验失败"}))
                return 1
            execute = bool(args.execute)
            if execute and not args.yes:
                preview = patcher.render_validation(validation)
                state = permission.PermissionState()
                decision = permission.request_intent({"op_type": "patch_apply"}, preview, state)
                execute = decision == permission.APPROVE
                if decision == permission.ABORT:
                    print("用户终止。")
                    return 1
            result = patcher.apply_spec(spec, root=args.root or ".", execute=execute)
            print(patcher.render_apply(result))
            return 0 if result["ok"] else 1
        if args.action == "tests":
            cmds = patcher.suggested_tests(args.root or ".")
            print(patcher.render_tests(cmds))
            return 0
        if args.action == "run-tests":
            cmd = args.test_command or "python -m pytest"
            result = patcher.run_test_command(cmd, root=args.root or ".", timeout=args.timeout)
            print(patcher.render_test_result(result))
            return 0 if result["ok"] else 1
    except Exception as e:  # noqa: BLE001
        print(f"patch 失败：{e}", file=sys.stderr)
        return 1
    return 2


def _cmd_code(args: argparse.Namespace) -> int:
    from . import code_agent

    try:
        root = args.root or "."
        if args.action == "plan":
            print(code_agent.render_plan(code_agent.task_plan(args.goal or "", root=root)))
            return 0
        if args.action == "context":
            print(code_agent.render_context(code_agent.context(args.goal or "", root=root, limit=args.limit)))
            return 0
        if args.action == "brief":
            print(code_agent.render_brief(code_agent.brief(args.goal or "", root=root, budget=args.budget)))
            return 0
        if args.action == "quality":
            print(code_agent.render_quality(code_agent.quality(root=root)))
            return 0
        if args.action == "bundle":
            if args.output_file:
                test_output = Path(args.output_file).expanduser().read_text(encoding="utf-8", errors="replace")
            else:
                test_output = args.text or ""
            print(code_agent.render_bundle(code_agent.task_bundle(args.goal or "", root=root, test_output=test_output, limit=args.limit)))
            return 0
        if args.action == "diff-brief":
            print(code_agent.render_diff_brief(code_agent.diff_brief(root=root, staged=args.staged)))
            return 0
        if args.action == "release-check":
            print(code_agent.render_release_check(code_agent.release_check(root=root, version=args.version or "")))
            return 0
        if args.action == "refs":
            print(code_agent.render_refs(code_agent.refs(args.goal or args.target or "", root=root, limit=args.limit)))
            return 0
        if args.action == "rename-plan":
            print(code_agent.render_rename_plan(code_agent.rename_plan(args.goal or args.target or "", args.new_name or "", root=root, limit=args.limit)))
            return 0
        if args.action == "run":
            provider = None
            if args.llm_patch and args.call:
                from .providers import from_settings
                provider = from_settings(config.get_model_config(), config.get_active_key())
            result = code_agent.run_loop(
                args.goal or "",
                root=root,
                test_command=args.test_command or "",
                run_tests_enabled=bool(args.run_tests),
                max_rounds=args.max_rounds,
                persist=True,
                llm_patch=bool(args.llm_patch),
                patch_provider=provider,
                timeout=args.timeout,
            )
            print(code_agent.render_run(result))
            return 0 if not result.get("test_result") or result["test_result"].get("ok") else 1
        if args.action == "apply-loop":
            from . import patcher, permission
            spec = patcher.load_spec(args.patch_spec or args.goal or "")
            execute = bool(args.execute)
            if execute and not args.yes:
                validation = patcher.validate_spec(spec, root=root)
                state = permission.PermissionState()
                decision = permission.request_intent({"op_type": "code.apply_loop"}, patcher.render_validation(validation), state)
                execute = decision == permission.APPROVE
                if decision == permission.ABORT:
                    print("用户终止。")
                    return 1
            result = code_agent.patch_apply_loop(
                spec,
                root=root,
                test_command=args.test_command or "",
                execute=execute,
                timeout=args.timeout,
                persist=True,
            )
            print(code_agent.render_run(result))
            return 0 if result.get("patch", {}).get("apply", {}).get("ok") and (not result.get("test_result") or result["test_result"].get("ok")) else 1
        if args.action == "runs":
            print(code_agent.render_run_list(code_agent.list_runs(limit=args.limit)))
            return 0
        if args.action == "show":
            print(code_agent.render_run(code_agent.load_run(args.goal or "")))
            return 0
        if args.action == "sandbox":
            print(code_agent.render_sandbox_plan(code_agent.sandbox_plan(root=root, name=args.name or "")))
            return 0
        if args.action == "test":
            result = code_agent.run_tests(args.test_command or "python -m pytest", root=root, timeout=args.timeout)
            print(code_agent.render_test_result(result))
            return 0 if result.get("ok") else 1
        if args.action == "repair":
            if args.output_file:
                text = Path(args.output_file).expanduser().read_text(encoding="utf-8", errors="replace")
            else:
                text = args.text or sys.stdin.read()
            print(code_agent.render_repair(code_agent.repair_plan(text, root=root)))
            return 0
        if args.action == "impact":
            print(code_agent.render_impact(code_agent.impact(args.goal or args.target or "", root=root)))
            return 0
        if args.action == "patch":
            if args.llm:
                provider = None
                if args.call:
                    from .providers import from_settings
                    provider = from_settings(config.get_model_config(), config.get_active_key())
                result = code_agent.llm_patch_candidate(
                    args.goal or "",
                    root=root,
                    path=args.path or "",
                    provider=provider,
                    timeout=args.timeout,
                )
            else:
                result = code_agent.patch_candidate(
                    args.goal or "",
                    root=root,
                    path=args.path or "",
                    old=args.old or "",
                    new=args.new or "",
                )
            print(code_agent.render_patch_candidate(result))
            return 0 if result.get("status") != "invalid" else 1
        if args.action == "review":
            print(code_agent.render_review(code_agent.review_ready(root=root, staged=args.staged)))
            return 0
    except Exception as e:  # noqa: BLE001
        print(f"code 失败：{e}", file=sys.stderr)
        return 1
    return 2
