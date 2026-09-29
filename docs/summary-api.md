# Summary API Contract

`GET /api/summary` is the additive reporting handoff for the Sentinel portal
and the future Grafana catalog. It is computed only from structured tables and
curated `reporting` views; it never parses the legacy activity display text.

The local development build always labels the document `simulation`. It does
not claim that Sentinel contacted an OLVM manager, SSH target, external secret provider,
Ansible, Grafana, or any other external service.

```json
{
  "generatedAt": "2026-09-28T10:00:00Z",
  "mode": {
    "kind": "simulation",
    "message": "Simulated development data only; no target or external service was contacted."
  },
  "freshness": {
    "state": "fresh | stale | unavailable",
    "latestCollectedAt": "2026-09-28T09:26:14Z",
    "ageSeconds": 2036,
    "staleAfterSeconds": 7200
  },
  "inventory": {
    "total": 12,
    "active": 12,
    "health": {"healthy": 9, "review": 2, "unreachable": 1, "unknown": 0},
    "sources": {"olvm": 10, "manual": 2}
  },
  "collections": {
    "latest": {
      "runId": "scheduled-0924",
      "name": "Scheduled inventory collection",
      "state": "completed | partial | unreachable | failed",
      "completedAt": "2026-09-28T09:26:14Z",
      "durationMs": 134000,
      "source": {
        "type": "inventory",
        "managerId": null,
        "profileId": "linux-inventory-facts",
        "profileName": "Linux inventory facts",
        "path": "inventory/linux-facts.yml",
        "commitSha": "immutable Git SHA or null",
        "state": "Git source state at the time the record was created"
      }
    },
    "outcomes": {
      "total": 12,
      "successful": 11,
      "unreachable": 1,
      "failed": 0,
      "queued": 0,
      "successRate": 91.67,
      "allUnreachable": false
    }
  },
  "capacity": {
    "hostsWithFacts": 11,
    "cpuCores": 52,
    "memory": {"totalBytes": 0, "usedBytes": 0, "utilizationPercent": 0},
    "disk": {"totalBytes": 0, "usedBytes": 0, "utilizationPercent": 0}
  },
  "alerts": {
    "openCount": 2,
    "bySeverity": {"critical": 1, "warning": 1, "info": 0},
    "items": ["structured alert records, newest first"]
  },
  "recentActivity": ["up to ten structured run records, newest first"]
}
```

`collections.latest` is `null` and capacity values are zero/null when no
completed structured run exists. `collections.outcomes.successRate` is `null`
when no hosts were attempted. A queued simulation is represented in
`recentActivity` but does not replace the latest completed collection.
