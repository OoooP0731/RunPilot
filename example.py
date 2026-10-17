"""Small, dependency-free Python task for the RunPilot README."""

import time


def main():
    for step in range(1, 6):
        print("step %d/5" % step, flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
