# apt-sources: why hoisting is gone

This example is the regression guard for the compiler's core decision:
nothing is hoisted or reordered. A compiler that collected `apt install`
lines into some "packages" section (cloud-init has one, and Ansible
inventories often grow one) would install `google-chrome-stable` before
the Chrome apt source exists below it. Here the whole file compiles to one
ordered task list, so the install stays exactly where it was written:

    apt update                 (task 1)
    apt install -y ripgrep     (task 2)
    echo deb ... chrome.list   (task 3)
    apt update                 (task 4)
    apt install -y google-chrome-stable  (step 5, after its source)
    apt clean                  (task 6)

Each step is stamped, so a crash at step 5 resumes at step 5, with the
source already in place.
