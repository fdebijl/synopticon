# To-do list

2. Speed up extraction

6. Reset no's on reviews

8. Align password fields to full length, add left margin

10. Add 'Last ran at ...' to pipeline operations in the web UI

12. Add toggle to Hide unnamed for all operations in review ui

15. UI should have a quick redirect to /login if not logged in, instead of waiting for the server

19. Ensure the health endpoint always responds when the container is live, still gets bogged down by jobs.

20. Bug on extract: /opt/venv/lib/python3.11/site-packages/PIL/Image.py:1136: UserWarning: Palette images with Transparency expressed in bytes should be converted to RGBA images

21. Scheduled jobs should have an indicator that they were ran from a schedule, ideally with a link back to the schedule

23. Investigate why jobs sometimes exit with code 9 (might be host oom'ing)

24. Guided onboarding: add initial extract and initial clustering (limited to 50 photos or so?)

27. Add object detection

31. Detect missing crops on review page and tell user to regen crops

32. Change default SCRFD and YOLO score to 0.45