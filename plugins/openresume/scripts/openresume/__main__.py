if __package__ in (None, ""):
    # Python can run this package directory from any working directory.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openresume import main


if __name__ == "__main__":
    main()
