# Auto Radar coverage

What the Auto Radar reads, how deeply, and what it cannot see. No scraper sees every posting, so the
employers it misses are named here with a link to check by hand.

## Read in depth (137 boards)

Every board in [`companies.csv`](companies.csv) is read on every run. **Actions → auto-boards** re-checks
each one weekly and reports any that stop answering.

| How a board is read | Employers |
|---|---|
| Newest jobs **and** a keyword search of the full job text | General Motors, Ford, Stellantis, Toyota, Nissan, Subaru, Mazda, Rivian, Slate, Polaris, Oshkosh, Daimler Truck, PACCAR, Cummins, Allison, John Deere, Caterpillar, AGCO, Volvo Cars, Volvo Group, International, Terex, Brunswick, Aptiv, Magna, BorgWarner, Lear, Denso, Valeo, Gentex, Astemo, Continental, Aumovio, Bosch, ZF, Schaeffler, Dana, Tenneco, PHINIA, Garrett, Stoneridge, Methode, Bendix, Adient, KOSTAL, Sensata, Littelfuse, Eaton, Nidec, Michelin, Hitachi, Moog, Woodward, Carrier, Rockwell Automation, GE Aerospace, Garmin, Panasonic, NXP, Renesas, Texas Instruments, onsemi, Microchip, Analog Devices, Qualcomm, NVIDIA, Ambarella, Infineon, Marvell, Allegro, AVL, Capgemini, Hyundai Motor Group (HATCI), Volkswagen Group, Doosan Bobcat, Crown, Wabtec |
| Every open job, with its full text | Lucid, Scout, Harbinger, Faraday Future, Mitsubishi Motors NA, Rivian–VW Technologies, Zoox, Aurora, Waymo, Nuro, Motional, May Mobility, Torc, Kodiak, Gatik, Stack AV, Outrider, Applied Intuition, Wayve, Waabi, Bot Auto, Pony.ai, WeRide, Plus, Aeva, Apex.AI, Sonatus, Woven by Toyota, Toyota Research Institute, Serve Robotics, Archer, GILLIG, Xos, LG Electronics, ALTEN, Autoliv, Honda, BRP, Bose, Vector North America, dSPACE, Hyundai AutoEver, ROUSH, Ouster, New Flyer |
| Every job by title, then the text of each engineering job (the site's keyword search is closed to automated readers) | BMW Group, CNH, Komatsu, TE Connectivity, thyssenkrupp |
| Newest jobs by title, then the text of each engineering job | Textron, Sumitomo Electric Wiring, Kubota, JTEKT, Joby |

About 6,600 other company boards (the DevOps radar's list) are also read by title; a generic title there is
kept only when the job text names the resume's tools.

Boards that answer but list no US jobs right now (still watched): Polestar, Wisk, Elektrobit, Volkswagen
Group of America's own board.

## Not covered: check these by hand

| Employer | Why | Careers page |
|---|---|---|
| Tesla | Blocks automated readers | https://www.tesla.com/careers/search/?query=embedded |
| MathWorks | Blocks automated readers | https://www.mathworks.com/company/jobs/opportunities/search?q=simulink |
| Mercedes-Benz (MBRDNA, MBUSA) | Own job system, no reader; its old board is empty | https://jobs.mercedes-benz.com/ |
| Visteon | Job board forbids automated reading (robots.txt) | https://www.visteon.com/careers/join-us/default.aspx |
| HARMAN | Board returns no jobs to automated readers | https://www.harman.com/careers |
| Forvia / Hella | Job site forbids automated reading | https://jobs.forvia.com/ |
| Mahle | Job search forbids automated reading | https://jobs.mahle.com/ |
| Aisin | ADP job system, no reader | https://www.aisinworld.com/careers/ |
| American Axle (Dauch) | Radancy job system, no reader | https://careers.dauch.com/en |
| Gentherm | Cornerstone job system, no reader | https://gentherm.com/en/careers/ |
| Marelli | Board not found | https://www.marelli.com/en/careers.html |
| Kia America | Board not found | https://www.kiacareers.com/ |
| Harley-Davidson | Board not found | https://www.harley-davidson.com/us/en/about-us/careers.html |
| VinFast | Board not found | https://vinfastauto.us/careers |
| Mobileye | Page built by script, no reader | https://careers.mobileye.com/ |
| IAV | Older SuccessFactors site, no reader | https://www.iav.com/career/ |
| KPIT | Board not found | https://www.kpit.com/careers-overview/ |
| L&T Technology Services | Board not found | https://www.ltts.com/careers |
| Tata Elxsi | Board not found | https://www.tataelxsi.com/careers |
| EDAG | Board not found | https://www.edag.com/en/career |
| Ricardo | Board not found | https://www.ricardo.com/careers |
| Preh | rexx job system, no reader | https://www.preh.com/en/career/job-offers |
| Dorleco | CEIPAL job system, no reader | https://dorleco.com/careers-at-dorleco/ |
| Apple | Own job system, no reader | https://jobs.apple.com/en-us/search?search=embedded |

Not reached at all (their sites did not answer, or no careers page was found): Yazaki, Hyundai Mobis,
HL Mando, Hanon Systems, Webasto, Brose (no US postings found), NSK, Mitsubishi Electric Automotive America,
Isuzu Technical Center, Karma, FEV, ETAS, Danlaw, New Eagle, LHP Engineering Solutions, Tata Technologies,
Bertrandt, Akkodis, Innoviz.

**Staffing and contract firms** are not covered: Actalent, Aerotek, Kelly Engineering, Belcan, RGBSI and
similar firms post many Michigan automotive embedded contracts on their own sites. The Michigan state job
bank (https://jobs.mitalent.org) also carries postings some employers list nowhere else.

## Limits worth knowing

- A board is read through the same feed its careers page uses. If an employer posts a job only on LinkedIn
  or fills it through a recruiter, it is not on the board and the radar cannot see it.
- Keyword searches return up to 100 results per keyword per board (US results where the board can filter by
  country). The 25 keywords overlap heavily, which is what makes a miss unlikely, not impossible.
- Clearance, citizenship, sponsorship and years-of-experience notes come from pattern matching on the job
  text and can be wrong. Skim the posting.
- If a job's text cannot be read after three tries, a target-title job is still listed (marked
  "description not read"); a generic-title job is not, because there is nothing to judge it on.
