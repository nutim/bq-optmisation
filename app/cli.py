"""Operational commands for Cloud Run jobs and local administration."""

import argparse
import json

from app.collector import ProjectCollector
from app.config import get_settings
from app.detectors import DetectorService
from app.logging import configure_logging
from app.store import get_store


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    collect = sub.add_parser("collect")
    collect.add_argument("--project")
    detect = sub.add_parser("detect")
    detect.add_argument("--project")
    args = parser.parse_args()

    configure_logging(get_settings().log_level)
    store = get_store()
    if args.command == "init":
        store.initialize()
        print(json.dumps({"status": "initialized"}))
    elif args.command == "collect":
        if args.project:
            project = store.get_project(args.project)
            if not project:
                print(json.dumps({"error": f"project not registered: {args.project}"}))
                raise SystemExit(1)
            projects = [project]
        else:
            projects = store.list_projects(enabled_only=True)
        output = {}
        for project in projects:
            if project and project["enabled"]:
                output[project["project_id"]] = ProjectCollector(store).collect(project)
        print(json.dumps(output, default=str))
    elif args.command == "detect":
        print(json.dumps(DetectorService(store).run(args.project), default=str))


if __name__ == "__main__":
    main()
