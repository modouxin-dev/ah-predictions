"""python -m ah <cmd> 便捷入口：render|settle|stats|site|telegram|consensus|prematch-record|backtest-record|sources|tg-bot|delivery|grounding|source-probe"""
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
else:
    print(__doc__)
