"""Репетиция контрольного часа через HTTP API (те же вызовы, что делает UI).

Запустите сервер (make run), затем:
    python scripts/rehearse_control_hour.py DEV p1-ru-001 p1-kz-001 p1-en-005 p4-ru-001 [--api http://localhost:8000]

1) оценка выбранных диалогов исходной версией; 2) замена c05 на шкалу 0/2/4/6 с N/A → новая версия →
применение (в LLM уходит только новый критерий); 3) смена веса + выключение критерия + новый flag только
для звонков → применение. Печатает время, число вызовов LLM и ответы. Создаёт новые версии чек-листа в БД.
"""
import argparse
import copy
import time

import httpx

ap = argparse.ArgumentParser()
ap.add_argument("group_id")
ap.add_argument("ids", nargs="+")
ap.add_argument("--api", default="http://localhost:8000")
ap.add_argument("--from-version", type=int, default=1)
a = ap.parse_args()
B = a.api.rstrip("/") + "/api"
IDS = a.ids
GROUP = a.group_id
c = httpx.Client(timeout=600)

def wait(job):
    t0 = time.time()
    while True:
        j = c.get(f"{B}/batches/{job['id']}").json()
        if j["status"] not in ("queued", "running"):
            return j, time.time() - t0
        time.sleep(1)

def run(label, **kw):
    r = c.post(f"{B}/batches", json={"group_id": GROUP, "conversation_ids": IDS, **kw})
    r.raise_for_status()
    j, dt = wait(r.json())
    print(f"[{label}] {j['status']} за {dt:.0f} с: ok={j['succeeded']} partial={j['partial']} skipped={j['skipped']} failed={j['failed']} вызовов LLM={j['llm_calls']} errors={j['errors'][:2]}")
    return j

def show(tag):
    for cid in IDS:
        e = c.get(f"{B}/evaluations/{GROUP}/{cid}").json()
        ans = " ".join(f"{a['criterion_id'][1:]}={a['answer'] if a['status']=='ok' else 'NR'}{'*' if a['source']=='llm' else ''}" for a in e["answers"])
        print(f"   {tag} {cid:10} v{e['checklist_version']} итог={e['total']:>6} {'GROSS ' if e['gross_error'] else ''}| {ans}")

j = run(f"1. исходная оценка v{a.from_version}", checklist_id="default_chat", checklist_version=a.from_version)
show(f"v{a.from_version}")

# 2. Замена c05 → новый критерий другого типа (как в редакторе: GET → правка → POST новой версии)
cl = c.get(f"{B}/checklists/default_chat", params={"version": a.from_version}).json()["checklist"]
new = copy.deepcopy(cl)
new["criteria"] = [{
    "id": "c09", "code": "05", "section": "conduct_closing", "title": "Проактивность",
    "description": "Оператор сам предложил полезную информацию или следующий шаг сверх прямого вопроса клиента.",
    "type": "scale", "weight": 15, "na_allowed": True,
    "na_condition": "Клиент завершил диалог сразу после ответа на свой вопрос.",
    "scale": [{"value": 6, "label": "Предложил сам, по делу и понятно"}, {"value": 4, "label": "Предложил, но общо или частично"},
              {"value": 2, "label": "Лишь намёк без конкретики"}, {"value": 0, "label": "Ничего не предложил при явной возможности"}],
    "guidance": "Засчитывай только инициативу оператора, а не ответ на прямой вопрос клиента. Реплики бота не учитываются.",
    "channels": ["agent chat", "voice call"], "requires_audio": False, "enabled": True,
} if x["id"] == "c05" else x for x in cl["criteria"]]
v = c.post(f"{B}/checklists/validate", json=new).json()
print("validate:", v["total_weight"], v["warnings"])
t0 = time.time()
saved = c.post(f"{B}/checklists", json=new).json()["checklist"]
d = c.get(f"{B}/checklists/default_chat/diff", params={"from_version": a.from_version, "to_version": saved["version"]}).json()
print(f"сохранено v{saved['version']}, diff:", [(x["criterion_id"], x["change"], x["llm"]) for x in d["changes"]])
j = run(f"2. применение v{saved['version']}", checklist_id="default_chat", checklist_version=saved["version"])
print(f"   от сохранения до готового результата: {time.time()-t0:.0f} с")
show(f"v{saved['version']}")

# 3. Вторая репетиция: смена веса + новый flag-критерий только для звонков
cl2 = copy.deepcopy(saved)
for x in cl2["criteria"]:
    if x["id"] == "c04": x["weight"] = 25
    if x["id"] == "c07": x["weight"] = 0; x["enabled"] = False
cl2["criteria"].append({"id": "c10", "section": "gross", "title": "Обещание гарантированной доходности", "type": "flag",
    "description": "Оператор гарантирует доход или прибыль от инвестиций.", "weight": 0,
    "flag_types": [{"id": "guaranteed_return", "label": "обещал гарантированный доход"}],
    "guidance": "Флаг только при прямом обещании гарантированного дохода.", "channels": ["voice call"]})
t0 = time.time()
s3 = c.post(f"{B}/checklists", json=cl2).json()
print("v3 warnings:", s3["warnings"])
s3 = s3["checklist"]
d = c.get(f"{B}/checklists/default_chat/diff", params={"from_version": saved["version"], "to_version": s3["version"]}).json()
print(f"сохранено v{s3['version']}, diff:", [(x["criterion_id"], x["change"], x["llm"]) for x in d["changes"]])
j = run(f"3. применение v{s3['version']}", checklist_id="default_chat", checklist_version=s3["version"])
print(f"   от сохранения до готового результата: {time.time()-t0:.0f} с")
show(f"v{s3['version']}")
