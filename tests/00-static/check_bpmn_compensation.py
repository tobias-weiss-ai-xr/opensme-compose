#!/usr/bin/env python3
"""
tests/00-static/check_bpmn_compensation.py — AF2: BPMN boundary compensation guard.

Validates that every AI/LLM ServiceTask in BPMN files has:
  1. A corresponding boundaryEvent attached to it
  2. The boundaryEvent references an error definition  
  3. A compensation handler ServiceTask reachable from boundaryEvent

Ensures AI failures => compensation paths (Epic AF).

Usage:  python3 tests/00-static/check_bpmn_compensation.py
Exit:   0 if all AI ServiceTasks have compensation; 1 otherwise
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
BPMN_DIR = ROOT / "bootstrap" / "bpmn"

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
OPERATON_NS = "http://operaton.org/schema/1.0/bpmn"

ET.register_namespace("", BPMN_NS)
ET.register_namespace("operaton", OPERATON_NS)


def get_ns(tag):
    return "{" + BPMN_NS + "}" + tag


def get_operaton(tag):
    return "{" + OPERATON_NS + "}" + tag


def is_ai_service_task(elem):
    tag = elem.tag
    if not tag.endswith("serviceTask"):
        return False
    name = (elem.get("name") or "").lower()
    cls = elem.get(get_operaton("class"), "").lower()
    expr = elem.get(get_operaton("expression"), "").lower()
    keywords = [" ai ", " ai-", " ai_", "llm", "categorize", "predict", "classify", "embed"]
    if any(k in name for k in keywords):
        return True
    if any(k in cls for k in keywords):
        return True
    if any(k in expr for k in keywords):
        return True
    return False


def check_bpmn_file(filepath, result):
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
    except ET.ParseError as e:
        result.fail(f"{filepath.name}: invalid XML - {e}")
        return

    elements_by_id = {e.get("id"): e for e in root.iter() if e.get("id")}
    ai_tasks = [e for e in root.iter() if is_ai_service_task(e)]
    if not ai_tasks:
        result.ok(f"{filepath.name}: no AI ServiceTasks found (ok)")
        return

    for task in ai_tasks:
        task_id = task.get("id")
        task_name = task.get("name", task_id)
        boundary_events = [
            e for e in root.iter()
            if e.tag.endswith("boundaryEvent") and e.get("attachedToRef") == task_id
        ]
        if not boundary_events:
            result.fail(
                f"{filepath.name}: AI ServiceTask '{task_name}' ({task_id}) has NO boundaryEvent"
            )
            continue
        for be in boundary_events:
            error_def = be.find(get_ns("errorEventDefinition"))
            if error_def is None:
                result.fail(f"{filepath.name}: boundaryEvent has no errorEventDefinition")
            outgoing = [
                sf for sf in root.iter()
                if sf.tag.endswith("sequenceFlow") and sf.get("sourceRef") == be.get("id")
            ]
            if not outgoing:
                result.fail(f"{filepath.name}: boundaryEvent has no outgoing flow")
                continue
            target_id = outgoing[0].get("targetRef")
            target = elements_by_id.get(target_id)
            if target is None:
                result.fail(f"{filepath.name}: boundaryEvent refs unknown '{target_id}'")
            elif not target.tag.endswith("serviceTask"):
                result.warn(f"{filepath.name}: target '{target_id}' not a ServiceTask")
            else:
                result.ok(
                    f"{filepath.name}: AI '{task_name}' -> boundary -> "
                    f"compensation '{target.get('name', target_id)}'"
                )


def main():
    result = Result("bpmn-compensation")
    result.header("AF2: BPMN boundary compensation check")

    if not BPMN_DIR.exists():
        result.fail(f"{BPMN_DIR} missing")
        return result.summary()

    for bpmn_file in sorted(BPMN_DIR.glob("*.bpmn")):
        check_bpmn_file(bpmn_file, result)

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
