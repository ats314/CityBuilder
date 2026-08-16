## What changed

<!-- The change itself, and why. If it corrects something previously claimed in
     README.md or docs/, say so explicitly — this repo records wrong turns. -->

## How it was verified

<!-- CI runs tools/smoke_test.py, which asserts the page draws. That is a floor,
     not a substitute: anything that affects pixels needs a rendered frame, and
     anything quantitative needs a number.

     python3 tools/smoke_test.py --shots shots
-->

- [ ] `tools/smoke_test.py` passes
- [ ] Rendered a frame and looked at it (attach it, if the change is visual)
- [ ] Measured, if the claim is quantitative

## Notes

<!-- Anything left undone, anything you are unsure about, anything a reviewer
     would otherwise have to discover for themselves. -->
