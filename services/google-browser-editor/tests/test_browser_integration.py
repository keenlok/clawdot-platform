from __future__ import annotations

import asyncio
import json
import ssl
from datetime import UTC, datetime, timedelta

import pytest
import trustme
from aiohttp import web
from chatcore.security.egress_proxy import EgressPolicy, PolicyOverrides
from chatcore_google_browser_editor import engine as engine_module
from chatcore_google_browser_editor.docs_ui import DocsUiAdapter
from chatcore_google_browser_editor.engine import BrowserEngine
from chatcore_google_browser_editor.journal import JobJournal
from chatcore_google_browser_editor.models import CreateJobRequest
from chatcore_google_browser_editor.settings import WorkerSettings
from chatcore_google_browser_editor.worker import WorkerFault, WorkerRuntime

pytestmark = pytest.mark.browser


@pytest.fixture
async def synthetic_editor(tmp_path):
    marker = {"journal": None, "job_id": None}
    state = {
        "spans": [{"text": "before", "bold": False}, {"text": " phrase", "bold": False}],
        "account": "editor@example.com",
    }
    ca = trustme.CA()
    cert = ca.issue_cert("editor.test")
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    cert.configure_cert(ssl_context)

    async def document(_request):
        account_email = state.get("account", "editor@example.com")
        popup_script = (
            "<script>window.open('/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit?tab=t.0','_blank');</script>"
            if state.get("popup")
            else ""
        )
        page = """<!doctype html><html><body tabindex="0">
<button aria-label="Google Account: ACCOUNT_EMAIL">Account</button>
<button>Editing</button><button aria-label="Bold" aria-pressed="false">Bold</button>
<div role="status">Saved to Drive</div>
<div role="status" aria-label="Current selection"></div>
<div role="textbox" aria-label="Document editor" tabindex="0" style="width:100px;height:40px"></div>
<button aria-label="Drift selection" hidden>Drift selection</button>
<div role="dialog" aria-label="Find and replace" hidden>
<input aria-label="Find"><input aria-label="Replace with">
<label><input type="checkbox" aria-label="Search all tabs">Search all tabs</label>
<label><input type="checkbox" aria-label="Match case">Match case</label>
<label><input type="checkbox" aria-label="Match using regular expressions">Regex</label>
<button>Replace</button><button>Done</button>
</div><div id="search-panel" hidden><input aria-label="Find in document">
<label><input type="checkbox" aria-label="Search all tabs">Search all tabs</label>
<label><input type="checkbox" aria-label="Match case">Match case</label>
<label><input type="checkbox" aria-label="Match using regular expressions">Regex</label></div>
<script>
(async()=>{window.doc=await (await fetch('/state')).json();
 const status=document.querySelector('[role=status]');
 const selection=document.querySelector('[aria-label="Current selection"]');
 const editor=document.querySelector('[aria-label="Document editor"]');
 const drift=document.querySelector('[aria-label="Drift selection"]');
 const dialog=document.querySelector('[role=dialog]');
 const find=dialog.querySelector('[aria-label=Find]');
 const replacement=dialog.querySelector('[aria-label="Replace with"]');
 const search=document.querySelector('[aria-label="Find in document"]');
 const searchPanel=document.querySelector('#search-panel');
 const bold=document.querySelector('[aria-label=Bold]');
 bold.setAttribute('aria-pressed',String(window.doc.bold));
 drift.hidden=!window.doc.enable_drift_control;
 drift.addEventListener('click',()=>{
  window.selectedText=' phrase';selection.textContent='Selected text:  phrase';editor.blur();
 });
 function content(){return window.doc.spans.map(span=>span.text).join('');}
 function count(q){return q ? content().split(q).length-1 : 0;}
 function result(q){
  const total=count(q);status.textContent='1 of '+total;
  window.selectedText=total===1&&!window.doc.selection_unavailable?q:null;
  selection.textContent=window.selectedText ? 'Selected text: '+window.selectedText : '';
  const span=window.doc.spans.find(item=>item.text===window.selectedText);
  bold.setAttribute('aria-pressed',String(span ? span.bold : false));
 }
 find.addEventListener('input',()=>result(find.value));
 search.addEventListener('input',()=>result(search.value));
 document.addEventListener('keydown',e=>{
 if(e.ctrlKey&&e.key.toLowerCase()==='h'){e.preventDefault();dialog.hidden=false;find.focus();}
 if(e.ctrlKey&&e.key.toLowerCase()==='f'){e.preventDefault();searchPanel.hidden=false;search.focus();}
 if(e.key==='Escape'){dialog.hidden=true;searchPanel.hidden=true;
  if(window.selectedText)editor.focus();else document.body.focus();}});
 dialog.querySelector('button').addEventListener('click',async()=>{
 if(count(find.value)!==1)return;
 await fetch('/replace',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({find:find.value,replacement:replacement.value})});
 status.textContent='Saved to Drive';});
 dialog.querySelectorAll('button')[1].addEventListener('click',()=>{dialog.hidden=true;});
 bold.addEventListener('click',async()=>{
 if(!window.selectedText)return;
 const targets=window.doc.spans.filter(span=>span.text===window.selectedText);
 if(targets.length!==1)return;
 targets[0].bold=!targets[0].bold;
 bold.setAttribute('aria-pressed',String(targets[0].bold));
 await fetch('/bold',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({text:window.selectedText,bold:targets[0].bold})});
 status.textContent='Saved to Drive';});
})();</script>POPUP_SCRIPT</body></html>"""
        page = page.replace("ACCOUNT_EMAIL", account_email).replace("POPUP_SCRIPT", popup_script)
        return web.Response(text=page, content_type="text/html")

    async def state_handler(_request):
        return web.json_response(state)

    async def replace_handler(request):
        body = await request.json()
        record = marker["journal"].get(marker["job_id"]) if marker["journal"] else None
        state["phase_at_mutation"] = record.phase if record else None
        text = "".join(span["text"] for span in state["spans"])
        text = text.replace(body["find"], body["replacement"], 1)
        state["spans"] = [{"text": text, "bold": False}]
        return web.json_response({"ok": True})

    async def bold_handler(request):
        body = await request.json()
        record = marker["journal"].get(marker["job_id"]) if marker["journal"] else None
        state["phase_at_mutation"] = record.phase if record else None
        target = next(span for span in state["spans"] if span["text"] == body["text"])
        target["bold"] = bool(body["bold"])
        state["saved"] = True
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/document/d/{file_id}/edit", document)
    app.router.add_get("/state", state_handler)
    app.router.add_post("/replace", replace_handler)
    app.router.add_post("/bold", bold_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_context)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    seed = tmp_path / "seed-state.json"
    seed.write_text(json.dumps({"cookies": [], "origins": []}))
    seed.chmod(0o600)
    secret = tmp_path / "secret"
    secret.write_text("x" * 48)
    secret.chmod(0o600)
    settings = WorkerSettings(
        _env_file=None,
        editor_email="editor@example.com",
        owner_email="owner@example.com",
        state_path=seed,
        secret_file=secret,
        db_path=tmp_path / "jobs.sqlite3",
    )

    async def resolver(host, _port):
        return ["127.0.0.1"]

    def policy():
        return EgressPolicy(
            resolver=resolver,
            overrides=PolicyOverrides(
                allowed_ports=frozenset({port}),
                allowed_loopback=frozenset({"127.0.0.1"}),
                ignore_cert_errors=True,
            ),
        )

    engine = BrowserEngine(
        settings, policy_factory=policy, editor_origin=f"https://editor.test:{port}"
    )
    yield state, engine, marker
    await runner.cleanup()


async def make_request(operation):
    return CreateJobRequest.model_validate(
        {
            "job_id": "a1b2c3d4e5f6478899aabbccddeeff00",
            "expires_at": datetime.now(UTC) + timedelta(seconds=90),
            "editor_email": "editor@example.com",
            "destination_file_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
            "destination_tab_id": "t.0",
            "expected_revision": "revision-opaque-01",
            "operation": operation,
        }
    )


@pytest.mark.asyncio
async def test_real_chromium_edits_synthetic_https_doc_through_guarded_proxy(synthetic_editor):
    state, engine, marker = synthetic_editor
    request = await make_request({"kind": "replace_text", "find": "before", "replacement": "after"})
    journal = JobJournal(engine.settings.db_path)
    marker["journal"] = journal
    marker["job_id"] = request.job_id
    runtime = WorkerRuntime(journal, engine, editor_email="editor@example.com")
    kind, _record = await runtime.submit(request)
    assert kind == "accepted"
    for _ in range(100):
        record = journal.get(request.job_id)
        if record and record.phase == "awaiting_verification":
            break
        await asyncio.sleep(0.02)
    assert record and record.phase == "awaiting_verification"
    assert "".join(span["text"] for span in state["spans"]) == "after phrase"
    assert state["phase_at_mutation"] == "running"
    await runtime.close()


@pytest.mark.asyncio
async def test_real_chromium_bold_operation_preserves_text(synthetic_editor):
    state, engine, marker = synthetic_editor
    request = await make_request({"kind": "bold_text", "text": "before", "bold": True})
    journal = JobJournal(engine.settings.db_path)
    marker["journal"] = journal
    marker["job_id"] = request.job_id
    runtime = WorkerRuntime(journal, engine, editor_email="editor@example.com")
    kind, _record = await runtime.submit(request)
    assert kind == "accepted"
    for _ in range(100):
        record = journal.get(request.job_id)
        if record and record.phase == "awaiting_verification":
            break
        await asyncio.sleep(0.02)
    assert record and record.phase == "awaiting_verification"
    assert "".join(span["text"] for span in state["spans"]) == "before phrase"
    assert state["spans"] == [
        {"text": "before", "bold": True},
        {"text": " phrase", "bold": False},
    ]
    assert state["phase_at_mutation"] == "running"
    await runtime.close()


@pytest.mark.asyncio
async def test_wrong_account_and_popup_stop_before_mutation(synthetic_editor):
    state, engine, _marker = synthetic_editor
    request = await make_request({"kind": "replace_text", "find": "before", "replacement": "after"})
    state["account"] = "owner@example.com"
    with pytest.raises(WorkerFault) as wrong_account:
        await engine.run(request, request.operation, asyncio.Event(), _noop)
    assert wrong_account.value.category == "wrong_editor_account"
    assert "".join(span["text"] for span in state["spans"]) == "before phrase"

    state["account"] = "editor@example.com"
    state["popup"] = True
    with pytest.raises(WorkerFault):
        await engine.run(request, request.operation, asyncio.Event(), _noop)
    assert "".join(span["text"] for span in state["spans"]) == "before phrase"


@pytest.mark.asyncio
async def test_real_chromium_refuses_bold_when_selection_is_not_accessible(synthetic_editor):
    state, engine, marker = synthetic_editor
    state["selection_unavailable"] = True
    request = await make_request({"kind": "bold_text", "text": "before", "bold": True})
    journal = JobJournal(engine.settings.db_path)
    marker["journal"] = journal
    marker["job_id"] = request.job_id
    runtime = WorkerRuntime(journal, engine, editor_email="editor@example.com")
    await runtime.submit(request)
    try:
        for _ in range(100):
            record = journal.get(request.job_id)
            if record and record.phase == "failed_before_edit":
                break
            await asyncio.sleep(0.02)
        assert record and record.phase == "failed_before_edit"
        assert record.error_category == "selection_unavailable"
        assert all(not span["bold"] for span in state["spans"])
        assert "phase_at_mutation" not in state
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_real_chromium_marks_focus_drift_uncertain_without_formatting(
    synthetic_editor, monkeypatch
):
    state, engine, marker = synthetic_editor
    state["enable_drift_control"] = True

    class DriftAfterJournalGuard(DocsUiAdapter):
        async def apply(self, operation, before_mutation):
            async def drift_after_guard():
                await before_mutation()
                await self.page.get_by_role("button", name="Drift selection", exact=True).click()

            await super().apply(operation, drift_after_guard)

    monkeypatch.setattr(engine_module, "DocsUiAdapter", DriftAfterJournalGuard)
    request = await make_request({"kind": "bold_text", "text": "before", "bold": True})
    journal = JobJournal(engine.settings.db_path)
    marker["journal"] = journal
    marker["job_id"] = request.job_id
    runtime = WorkerRuntime(journal, engine, editor_email="editor@example.com")
    await runtime.submit(request)
    try:
        for _ in range(100):
            record = journal.get(request.job_id)
            if record and record.phase == "uncertain":
                break
            await asyncio.sleep(0.02)
        assert record and record.phase == "uncertain"
        assert record.error_category == "selection_unavailable"
        assert all(not span["bold"] for span in state["spans"])
        assert "phase_at_mutation" not in state
    finally:
        await runtime.close()


async def _noop():
    return None
