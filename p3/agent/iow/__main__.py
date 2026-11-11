import os

import uvicorn

from iow.api import app

if __name__ == "__main__":
    # Access logs off: the agent prints its own short, tagged lines.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("IOW_PORT", "8042")),
                log_level="warning", access_log=False)
