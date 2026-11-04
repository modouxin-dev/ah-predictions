"""python -m ah <cmd> 便捷入口：render|settle|stats|site|telegram"""
import sys

cmd, sys.argv = (sys.argv[1] if len(sys.argv) > 1 else "help"), [sys.argv[0]] + sys.argv[2:]
if cmd == "render":
    from .render_report import main; main()
elif cmd == "settle":
    from .settle import main; main()
elif cmd == "stats":
    from .stats import main; main()
elif cmd == "site":
    from .build_site import build; print(build())
elif cmd == "telegram":
    from .publish_telegram import main; main()
else:
    print(__doc__)
