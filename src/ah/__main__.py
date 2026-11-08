"""python -m ah <cmd> 便捷入口：render|settle|stats|site|telegram|consensus|prematch-record|backtest-record|sources|tg-bot|delivery|grounding|source-probe|samples|factors|backtest|gaps|review|runfacts|source-audit|delivery-gaps|date-consistency|data-quality|ops-failures"""
import sys

cmd, sys.argv = (sys.argv[1] if len(sys.argv) > 1 else "help"), [sys.argv[0]] + sys.argv[2:]
if cmd == "render":
    from .render_report import main; main()
elif cmd == "settle":
    from .settle import main; main()
elif cmd == "stats":
    from .stats import main; main()
elif cmd == "site":
    from .build_site import main; main()
elif cmd == "telegram":
    from .publish_telegram import main; main()
elif cmd == "consensus":
    from .consensus import main; main()
elif cmd == "prematch-record":
    from .records import prematch_main; prematch_main()
elif cmd == "backtest-record":
    from .records import backtest_main; backtest_main()
elif cmd == "sources":
    from .source_status import main; main()
elif cmd == "tg-bot":
    from .tg_bot import main; main()
elif cmd == "delivery":
    from .delivery import main; main()
elif cmd == "grounding":
    from .grounding import main; main()
elif cmd == "source-probe":
    from .source_probe import main; main()
elif cmd == "samples":
    from .samples import main; main()
elif cmd == "factors":
    from .factors import main; main()
elif cmd == "backtest":
    from .backtest import main; main()
elif cmd == "gaps":
    from .sample_gaps import cli_main; cli_main()
elif cmd == "review":
    from .review import main; main()
elif cmd == "runfacts":
    from .runfacts import main; main()
elif cmd == "source-audit":
    from .source_audit import main; main()
elif cmd == "delivery-gaps":
    from .delivery_gaps import main; main()
elif cmd == "date-consistency":
    from .date_consistency import main; main()
elif cmd == "data-quality":
    from .data_quality import main; main()
elif cmd == "ops-failures":
    from .ops_failures import main; main()
else:
    print(__doc__)
