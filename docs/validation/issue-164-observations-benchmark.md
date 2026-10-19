# Optional storage observations: actual XLSX calibration

```json
{
  "schema_version": 1,
  "measured_at": "2026-10-19T13:33:03.010592+00:00",
  "environment": {
    "python": "3.12.13",
    "sqlite": "3.53.1",
    "openpyxl": "3.1.5",
    "platform": "Linux-6.12.111+deb13-amd64-x86_64-with-glibc2.41",
    "machine": "x86_64"
  },
  "workload": {
    "sheets": 5,
    "rows_per_sheet": 1000,
    "cells": 25000,
    "input_bytes": 149020,
    "core_ledger": "always_enabled",
    "timed_scope": "execution_and_stage_commits_plus_actual_xlsx_parsing",
    "warmup_pairs": 1,
    "measured_pairs": 7
  },
  "disabled": {
    "samples": 7,
    "median_seconds": 0.5365743120200932,
    "p95_seconds": 0.5470496250782162,
    "maximum_seconds": 0.5470496250782162
  },
  "enabled": {
    "samples": 7,
    "median_seconds": 0.5429985669907182,
    "p95_seconds": 0.558468107949011,
    "maximum_seconds": 0.558468107949011
  },
  "median_overhead_seconds": 0.006424254970625043,
  "median_overhead_percent": 1.197272181450325,
  "initial_target_percent": 5,
  "within_initial_target": true,
  "interpretation": "One host/workload; signed negative differences are measurement noise.",
  "samples": [
    {
      "enabled": true,
      "seconds": 0.44172617606818676,
      "output_digest": "aa230be11520f1d08e1509454cff96a45cd592a250eeebd8c35fead571b301fe",
      "output_bytes": 6971089,
      "cells": 25000
    },
    {
      "enabled": false,
      "seconds": 0.5470496250782162,
      "output_digest": "aa230be11520f1d08e1509454cff96a45cd592a250eeebd8c35fead571b301fe",
      "output_bytes": 6971089,
      "cells": 25000
    },
    {
      "enabled": false,
      "seconds": 0.5436047639232129,
      "output_digest": "aa230be11520f1d08e1509454cff96a45cd592a250eeebd8c35fead571b301fe",
      "output_bytes": 6971089,
      "cells": 25000
    },
    {
      "enabled": true,
      "seconds": 0.4392861690139398,
      "output_digest": "aa230be11520f1d08e1509454cff96a45cd592a250eeebd8c35fead571b301fe",
      "output_bytes": 6971089,
      "cells": 25000
    },
    {
      "enabled": true,
      "seconds": 0.558468107949011,
      "output_digest": "aa230be11520f1d08e1509454cff96a45cd592a250eeebd8c35fead571b301fe",
      "output_bytes": 6971089,
      "cells": 25000
    },
    {
      "enabled": false,
      "seconds": 0.5365743120200932,
      "output_digest": "aa230be11520f1d08e1509454cff96a45cd592a250eeebd8c35fead571b301fe",
      "output_bytes": 6971089,
      "cells": 25000
    },
    {
      "enabled": false,
      "seconds": 0.43856302194762975,
      "output_digest": "aa230be11520f1d08e1509454cff96a45cd592a250eeebd8c35fead571b301fe",
      "output_bytes": 6971089,
      "cells": 25000
    },
    {
      "enabled": true,
      "seconds": 0.5429985669907182,
      "output_digest": "aa230be11520f1d08e1509454cff96a45cd592a250eeebd8c35fead571b301fe",
      "output_bytes": 6971089,
      "cells": 25000
    },
    {
      "enabled": true,
      "seconds": 0.5523547030752525,
      "output_digest": "aa230be11520f1d08e1509454cff96a45cd592a250eeebd8c35fead571b301fe",
      "output_bytes": 6971089,
      "cells": 25000
    },
    {
      "enabled": false,
      "seconds": 0.4326377479592338,
      "output_digest": "aa230be11520f1d08e1509454cff96a45cd592a250eeebd8c35fead571b301fe",
      "output_bytes": 6971089,
      "cells": 25000
    },
    {
      "enabled": false,
      "seconds": 0.5358121930621564,
      "output_digest": "aa230be11520f1d08e1509454cff96a45cd592a250eeebd8c35fead571b301fe",
      "output_bytes": 6971089,
      "cells": 25000
    },
    {
      "enabled": true,
      "seconds": 0.5473902620142326,
      "output_digest": "aa230be11520f1d08e1509454cff96a45cd592a250eeebd8c35fead571b301fe",
      "output_bytes": 6971089,
      "cells": 25000
    },
    {
      "enabled": true,
      "seconds": 0.4412779799895361,
      "output_digest": "aa230be11520f1d08e1509454cff96a45cd592a250eeebd8c35fead571b301fe",
      "output_bytes": 6971089,
      "cells": 25000
    },
    {
      "enabled": false,
      "seconds": 0.5412734630517662,
      "output_digest": "aa230be11520f1d08e1509454cff96a45cd592a250eeebd8c35fead571b301fe",
      "output_bytes": 6971089,
      "cells": 25000
    }
  ]
}
```
