// Real responses captured from the live orchestrator (curl against POST /query and
// POST /query/stream on the running Phase 5 stack: postgres with 221 chunks / 14 sources,
// qwen3.5-8k via Ollama), one per response_type, used ONLY by the ?mock=<state> query param
// (see app/page.tsx) so every state is reviewable without waiting 20-140s per screenshot.
// Never read on the normal path -- streamQuery/getSourcesStatus in lib/api.ts are the only
// functions that call the real backend.
//
// four of these five (answer, refusal_advice, clarify, no_answer) are byte-identical to what
// POST /query returned for a real question against the real corpus and the real generator.
// blocked_unverified could not be captured that way: across several real questions the
// generator always cited a valid index, so the citation-verification guardrail never
// organically failed. That fixture instead comes from running the real
// app.pipeline.answer_question against the real database and a real embedding, with only the
// LLM call itself swapped for a fixed string that cites an out-of-range index -- every other
// field (citations, contexts, freshness) is real, retrieved data, not invented.
import type { AnswerResponse } from './api';

export const MOCK_ANSWER: AnswerResponse = {
  "answer": "The STEM OPT extension lasts 24 months [1]. This additional period of employment authorization applies to F-1 students who meet specific eligibility requirements, such as having a degree included on the DHS STEM Designated Degree Program List and working for an employer enrolled in E-Verify [3] or being employed by an employer using E-Verify with an initial grant of post-completion OPT based on that degree [5].",
  "citations": [
    {
      "source_url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt",
      "chunk_id": 441,
      "snippet": "Optional Practical Training Extension for STEM Students (STEM OPT) > Eligibility for the STEM OPT Extension To qualify for the 24-month extension, you must: - Have been granted OPT and currently be in a valid period of post-completion OPT;..."
    },
    {
      "source_url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt",
      "chunk_id": 442,
      "snippet": "Optional Practical Training Extension for STEM Students (STEM OPT) > Applying for a STEM OPT Extension To apply for an extension, you must properly file: - Form I-765 with - The correct application fee; - Your employer’s name as listed in..."
    },
    {
      "source_url": "https://www.uscis.gov/working-in-the-united-states/temporary-workers/h-1b-specialty-occupations/extension-of-post-completion-optional-practical-training-opt-and-f-1-status-for-eligible-students",
      "chunk_id": 511,
      "snippet": "Extension of Post Completion Optional Practical Training (OPT) and F-1 Status for Eligible Students under the H-1B Cap-Gap Regulations > STEM OPT Extensions F-1 students who receive science, technology, engineering, and mathematics (STEM)..."
    },
    {
      "source_url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt",
      "chunk_id": 443,
      "snippet": "Optional Practical Training Extension for STEM Students (STEM OPT) > After Receiving a STEM OPT Extension **Student Reporting Responsibilities** If you receive a STEM OPT extension, you must: - Report changes to the following information..."
    },
    {
      "source_url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-opt-for-f-1-students",
      "chunk_id": 435,
      "snippet": "Optional Practical Training (OPT) for F-1 Students > STEM OPT Extension If you have earned a degree in certain Science, Technology, Engineering and Mathematics (STEM) fields, you may apply for a 24-month extension of your post-completion..."
    }
  ],
  "contexts": [
    {
      "chunk_id": 441,
      "source_url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt",
      "section_heading": "Eligibility for the STEM OPT Extension",
      "content": "Optional Practical Training Extension for STEM Students (STEM OPT) > Eligibility for the STEM OPT Extension\n\nTo qualify for the 24-month extension, you must:\n\n- Have been granted OPT and currently be in a valid period of post-completion OPT;\n- Have earned a bachelor’s, master’s, or doctoral degree from a school that is accredited by a U.S. Department of Education-recognized accrediting agency and is certified by [the Student and Exchange Visitor Program (SEVP)](https://studyinthestates.dhs.gov/school-search) at the time you submit your STEM OPT extension application.\n  - **NOTE: Previously obtained STEM degrees**: If you are an F-1 student participating in a 12-month period of post-completion OPT based on a non-STEM degree, you may be eligible to use a previous STEM degree from a U.S. institution of higher education to apply for a STEM OPT extension. You must have received both degrees from currently accredited and SEVP-certified institutions, and cannot have already received a STEM OPT extension based on this previous degree. The practical training opportunity also must be directly related to the previously obtained STEM degree.\n    - For example, if the student is currently participating in OPT based on a non-STEM  degree, but previously received a bachelor’s degree in a degree program  that appears on the current [DHS STEM Designated Degree Program List](https://www.ice.gov/sevis/schools#dhs-stem-designated-degree-program-list-and-cip-code-nomination-process), the student may be able to apply for a STEM OPT extension based on the bachelor’s degree as long as it is from an accredited U.S. college or university and the OPT employment opportunity is directly related to the bachelor’s STEM degree.\n  - **NOTE: STEM degrees you obtain in the future**: If you enroll in a new academic program in the future and earn another qualifying STEM degree at a higher educational level, you may be eligible for one additional 24-month STEM OPT extension.\n    - For example: If you receive a 24-month STEM OPT extension based on a qualifying bachelor’s degree  and you later earn a qualifying master’s degree, you may apply for an additional 24-month STEM OPT extension based on your master’s degree.\n- Work for an employer who meets all the requirements listed below in the STEM OPT Employer Responsibilities section; and\n- Submit the [Form I-765, Application for Employment Authorization](/i-765), up to 90 days before your current OPT employment authorization expires, and within 60 days of the date your designated school official (DSO) enters the recommendation for OPT into your Student and Exchange Visitor Information System (SEVIS) record."
    },
    {
      "chunk_id": 442,
      "source_url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt",
      "section_heading": "Applying for a STEM OPT Extension",
      "content": "Optional Practical Training Extension for STEM Students (STEM OPT) > Applying for a STEM OPT Extension\n\nTo apply for an extension, you must properly file:\n\n- Form I-765 with\n  - The correct application fee;\n  - Your employer’s name as listed in E-Verify, and\n  - Your employer’s E-Verify Company Identification Number or valid E-Verify Client Company Identification Number\n- Form I-20, Certificate of Eligibility for Nonimmigrant Student Status, endorsed by your DSO within the last 60 days; and\n- A copy of your STEM degree.\n\nIf you file your STEM OPT extension application on time and your OPT period expires while your extension application is pending, we will automatically extend your employment authorization for 180 days. This automatic 180-day extension ceases once USCIS adjudicates your STEM OPT extension application."
    },
    {
      "chunk_id": 511,
      "source_url": "https://www.uscis.gov/working-in-the-united-states/temporary-workers/h-1b-specialty-occupations/extension-of-post-completion-optional-practical-training-opt-and-f-1-status-for-eligible-students",
      "section_heading": "STEM OPT Extensions",
      "content": "Extension of Post Completion Optional Practical Training (OPT) and F-1 Status for Eligible Students under the H-1B Cap-Gap Regulations > STEM OPT Extensions\n\nF-1 students who receive science, technology, engineering, and mathematics (STEM) degrees included on the [STEM Designated Degree Program List (PDF)](https://www.ice.gov/sites/default/files/documents/Document/2016/stem-list.pdf), are employed by employers enrolled in and maintain good standing with E-Verify, and who have received an initial grant of post-completion OPT employment authorization related to such a degree, may apply for a 24-month extension of such authorization. F-1 students may obtain additional information about STEM OPT extensions in the [USCIS Policy Manual](/policy-manual/volume-2-part-f), on our [Optional Practical Training Extension for STEM Students (STEM OPT)](/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt) page, or the [STEM OPT Hub](https://studyinthestates.dhs.gov/stem-opt-hub).\n\nStudents who are eligible for a cap-gap extension of post-completion OPT employment and F-1 status may apply for a STEM OPT extension during the cap-gap extension period.\n\nHowever, students may not apply for a STEM OPT extension once the cap-gap extension period is terminated (if the H-1B petition is rejected, denied, revoked, or withdrawn) and the student has entered the 60-day grace period."
    },
    {
      "chunk_id": 443,
      "source_url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt",
      "section_heading": "After Receiving a STEM OPT Extension",
      "content": "Optional Practical Training Extension for STEM Students (STEM OPT) > After Receiving a STEM OPT Extension\n\n**Student Reporting Responsibilities**\n\nIf you receive a STEM OPT extension, you must:\n\n- Report changes to the following information to your DSO within 10 days of the change, specifically:\n  - Your legal name;\n  - Your residential or mailing address;\n  - Your email address;\n  - Your employer’s name; and\n  - Your employer’s address.\n- Report to your DSO every 6 months to confirm the information listed above, even if none of your information has changed.\n\nFor more information, please refer to the [USCIS Policy Manual](https://www.uscis.gov/policy-manual/volume-2-part-f) and the [DHS STEM OPT Hub](https://studyinthestates.dhs.gov/stem-opt-hub).\n\n**Unemployment during the OPT Period**\n\nYou may be unemployed during your OPT period for a limited number of days.\n\n| If you received… | You may be unemployed for… | For a total of…(during the OPT period) |\n| --- | --- | --- |\n| Initial post-completion OPT only | Up to 90 days | 90 days |\n| 24-month extension | An additional 60 days | 150 days |"
    },
    {
      "chunk_id": 435,
      "source_url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-opt-for-f-1-students",
      "section_heading": "STEM OPT Extension",
      "content": "Optional Practical Training (OPT) for F-1 Students > STEM OPT Extension\n\nIf you have earned a degree in certain Science, Technology, Engineering and Mathematics (STEM) fields, you may apply for a 24-month extension of your post-completion OPT employment authorization if you:\n\n- Are an F-1 student who received a STEM degree included on the [STEM Designated Degree Program List (PDF)](https://www.ice.gov/sites/default/files/documents/Document/2016/stem-list.pdf);\n- Are employed by an employer who is enrolled in and is using E-Verify; and\n- Received an initial grant of post-completion OPT employment authorization based on your STEM degree.\n\nIf you are interested in applying for a STEM OPT extension, please see our [Optional Practical Training Extension for STEM Students (STEM OPT)](/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt) page for more information."
    }
  ],
  "disclaimer": "This is an unofficial tool. It is not legal advice and is not affiliated with USCIS or DHS. For guidance on your own situation, talk to your DSO or a licensed immigration attorney.",
  "generated_at": "2026-09-06T05:43:00.271865Z",
  "response_type": "answer",
  "refusal_reason": null,
  "freshness": {
    "as_of": "2026-09-06",
    "sources": [
      {
        "source_url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt",
        "page_last_updated": "2026-01-30",
        "last_verified_at": "2026-09-06T03:25:39.086585Z",
        "fetched_at": "2026-08-29T02:01:34.469550Z",
        "rule_effective_date": null
      },
      {
        "source_url": "https://www.uscis.gov/working-in-the-united-states/temporary-workers/h-1b-specialty-occupations/extension-of-post-completion-optional-practical-training-opt-and-f-1-status-for-eligible-students",
        "page_last_updated": "2025-01-17",
        "last_verified_at": "2026-09-06T03:26:34.945049Z",
        "fetched_at": "2026-08-29T02:01:42.661266Z",
        "rule_effective_date": null
      },
      {
        "source_url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-opt-for-f-1-students",
        "page_last_updated": "2024-11-25",
        "last_verified_at": "2026-09-06T03:25:37.253785Z",
        "fetched_at": "2026-08-29T02:01:32.865828Z",
        "rule_effective_date": null
      }
    ],
    "notices": []
  }
};

export const MOCK_REFUSAL_ADVICE: AnswerResponse = {
  "answer": "You may be eligible for an automatic extension of your F-1 status and employment authorization because you are maintaining valid status on April 1 while your employer files a timely H-1B petition [2]. This \"cap-gap\" period applies when the H-1B start date falls between October 1 and April 1 of the following year, which covers a standard October start date after an OPT expiration in July or earlier [3]. However, you will only receive this extension if your employer files the cap-subject petition during the filing window with a valid registration based on being selected for that fiscal year, as required by law [2][4]. If USCIS receives and approves (or selects) the petition while it is pending, your authorized period of OPT employment continues automatically until April 1 or until the H-1B effective date begins earlier in October [3]. You cannot receive this automatic extension if your employer withdraws the petition before you change status or does not select you for registration when required to meet quota limits.\n\nFor advice on your own situation, talk to your DSO or a licensed immigration attorney.",
  "citations": [
    {
      "source_url": "https://www.uscis.gov/working-in-the-united-states/temporary-workers/h-1b-specialty-occupations/extension-of-post-completion-optional-practical-training-opt-and-f-1-status-for-eligible-students",
      "chunk_id": 504,
      "snippet": "Extension of Post Completion Optional Practical Training (OPT) and F-1 Status for Eligible Students under the H-1B Cap-Gap Regulations The “cap-gap” period starts when an F-1 student’s status and employment authorization expires and,..."
    },
    {
      "source_url": "https://studyinthestates.dhs.gov/students/complete/h-1b-status-and-the-cap-gap-extension",
      "chunk_id": 520,
      "snippet": "H-1B Status and the Cap Gap Extension > Cap Gap Extension Eligibility This means an F-1 student filing for H1-B status on April 1 with a benefit start date between Oct. 1 and April 1 of the following year may qualify for an extension of..."
    },
    {
      "source_url": "https://www.uscis.gov/working-in-the-united-states/temporary-workers/h-1b-specialty-occupations/extension-of-post-completion-optional-practical-training-opt-and-f-1-status-for-eligible-students",
      "chunk_id": 506,
      "snippet": "Extension of Post Completion Optional Practical Training (OPT) and F-1 Status for Eligible Students under the H-1B Cap-Gap Regulations > Eligibility for an Extension Cap-subject H-1B petitions that are properly and timely filed for an..."
    },
    {
      "source_url": "https://www.uscis.gov/working-in-the-united-states/temporary-workers/h-1b-specialty-occupations/extension-of-post-completion-optional-practical-training-opt-and-f-1-status-for-eligible-students",
      "chunk_id": 514,
      "snippet": "Extension of Post Completion Optional Practical Training (OPT) and F-1 Status for Eligible Students under the H-1B Cap-Gap Regulations > Changes in Employment - Laid off or terminated by the H-1B employer: If the student has been approved..."
    },
    {
      "source_url": "https://studyinthestates.dhs.gov/students/complete/h-1b-status-and-the-cap-gap-extension",
      "chunk_id": 516,
      "snippet": "H-1B Status and the Cap Gap Extension Last updated: April 28, 2025 The H-1B status is temporary employment authorization for a nonimmigrant who performs services in a specialty occupation. An employer may petition [United States..."
    }
  ],
  "contexts": [
    {
      "chunk_id": 504,
      "source_url": "https://www.uscis.gov/working-in-the-united-states/temporary-workers/h-1b-specialty-occupations/extension-of-post-completion-optional-practical-training-opt-and-f-1-status-for-eligible-students",
      "section_heading": "Extension of Post Completion Optional Practical Training (OPT) and F-1 Status for Eligible Students under the H-1B Cap-Gap Regulations",
      "content": "Extension of Post Completion Optional Practical Training (OPT) and F-1 Status for Eligible Students under the H-1B Cap-Gap Regulations\n\nThe “cap-gap” period starts when an F-1 student’s status and employment authorization expires and, unless terminated, ends on April 1 of the fiscal year for which the H-1B status is being requested or until the validity start date of the approved petition, whichever is earlier.\n\nCap-gap occurs because an employer may not file, and USCIS may not accept, a cap subject H-1B petition submitted more than 6 months in advance of the date of actual need for the beneficiary’s services or training. As a result, the earliest date that an employer can file an H-1B cap-subject petition is April 1 for the following fiscal year, which starts Oct. 1. If USCIS approves the H-1B petition and the accompanying change of status request, the earliest date that the student may start the approved H-1B employment is Oct. 1, but the beneficiary’s OPT employment authorization may expire before the H-1B start date."
    },
    {
      "chunk_id": 520,
      "source_url": "https://studyinthestates.dhs.gov/students/complete/h-1b-status-and-the-cap-gap-extension",
      "section_heading": "Cap Gap Extension Eligibility",
      "content": "H-1B Status and the Cap Gap Extension > Cap Gap Extension Eligibility\n\nThis means an F-1 student filing for H1-B status on April 1 with a benefit start date between Oct. 1 and April 1 of the following year may qualify for an extension of status and/or employment authorization. The general eligibility requirements for the cap gap extension are listed below, but please understand that your individual case may be different and you should always talk with your designated school official (DSO) about whether you would qualify for a cap gap extension. Additionally, always maintain regular contact with your potential employer to receive updates on the status of your H-1B petition, should they file one for you.\n\nAs an F-1 student, you may be eligible for an extension of your F-1 status through April 1 of the following year if you meet the following requirements:\n\n- Your potential employer files an H-1B petition in a timely manner with USCIS with an employment start date between Oct. 1 and April 1 of the following year.\n- You are maintaining your F-1 status on the date your potential employer files your H-1B petition.\n- USCIS receives the H-1B petition in a timely manner and issues a receipt for it.\n\nYou may be eligible for an extension of your F-1 status and authorized period of post-completion OPT (including [the science, technology, engineering and mathematics (STEM) OPT extension](https://studyinthestates.dhs.gov/stem-opt-extension-overview)) in the following circumstances:\n\n- Your employer files an H-1B petition in a timely manner with USCIS with an employment start date between Oct. 1 and April 1 of the following year.\n- You are maintaining your F-1 status on the date your potential employer files your H-1B petition.\n- You are in an authorized period of post-completion OPT (including the STEM OPT extension) on the date your potential employer files your H-1B petition.\n- USCIS receives the H-1B petition in a timely manner and issues a receipt for it.\n\nIf you are eligible for the cap gap extension, your proof that you are still in F-1 status and that you may continue OPT (if applicable) is a note on your Form I-20, “Certificate of Eligibility of [Nonimmigrant](https://studyinthestates.dhs.gov/glossary/N/#Nonimmigrant \"An alien who is admitted to the United States for a specific temporary period of time.\") Student Status.” Obtain an updated Form I-20 from your DSO when the Cap Gap extension begins with a note indicating that your F-1 status and, if applicable, your OPT authorization will continue, typically until April 1 of the following year.\n\nIf your H-1B petition is denied, withdrawn, revoked or not selected, an F-1 student will have the standard 60-day grace period from the date of the rejection notice or their program or OPT end date, whichever is later, to depart the United States."
    },
    {
      "chunk_id": 506,
      "source_url": "https://www.uscis.gov/working-in-the-united-states/temporary-workers/h-1b-specialty-occupations/extension-of-post-completion-optional-practical-training-opt-and-f-1-status-for-eligible-students",
      "section_heading": "Eligibility for an Extension",
      "content": "Extension of Post Completion Optional Practical Training (OPT) and F-1 Status for Eligible Students under the H-1B Cap-Gap Regulations > Eligibility for an Extension\n\nCap-subject H-1B petitions that are properly and timely filed for an eligible F-1 student that request a change of status to H-1B within the fiscal year for which status is being requested qualify for a cap-gap extension.\n\nTimely filed means that the H-1B petition (indicating change of status rather than consular processing) was filed during the applicable H-1B filing period, which begins April 1 and while the student's authorized F-1 duration of status (D/S) admission was still in effect (including any period of time during the academic course of study, any authorized periods of post-completion optional practical training (OPT), and the 60-day departure preparation period commonly known as the \"grace period\"). A cap-subject H-1B petition will not be considered to be properly filed unless it is based on a valid, selected registration for the same beneficiary and the appropriate fiscal year, unless the registration requirement is suspended.\n\nOnce the petitioner properly and timely files a request to change status to H-1B within the fiscal year for which such status is being requested, the automatic cap-gap extension will begin. If the student’s H-1B petition is approved (or selected and approved if the registration requirement is suspended), the student’s cap-gap extension of status will continue until April 1 of the fiscal year for which such H-1B status is being requested or until the validity start date of the approved petition, whichever is earlier. The cap-gap extension of status will automatically terminate if the student’s H-1B petition is denied, withdrawn, revoked, rejected, or is not selected, or if the change of status request is denied or withdrawn even if the H-1B petition is approved for consular processing. The student will have the standard 60-day grace period from the date the extension of status terminated or their program end date, whichever is later, to depart the United States (however, the 60-day grace period does not apply to an F-1 student whose accompanying change of status request is denied or revoked due to a status violation, misrepresentation, or fraud).\n\nStudents are strongly encouraged to stay in close communication with their petitioning employer during the cap-gap extension period for status updates on the H-1B petition processing.\n\n**Please note:** F-1 students who have entered the 60-day grace period are not authorized to work. If an H 1B cap-subject petition is properly filed for a student who has entered the 60-day grace period, the student will receive the automatic extension of their F-1 status, but will not be authorized to work since the student was not authorized to work at the time H-1B petition was filed."
    },
    {
      "chunk_id": 514,
      "source_url": "https://www.uscis.gov/working-in-the-united-states/temporary-workers/h-1b-specialty-occupations/extension-of-post-completion-optional-practical-training-opt-and-f-1-status-for-eligible-students",
      "section_heading": "Changes in Employment",
      "content": "Extension of Post Completion Optional Practical Training (OPT) and F-1 Status for Eligible Students under the H-1B Cap-Gap Regulations > Changes in Employment\n\n- Laid off or terminated by the H-1B employer: If the student has been approved to change their status to an H-1B nonimmigrant but is laid off/terminated by the H-1B employer before the date they officially obtain H-1B status, the student can retrieve any unused OPT if they have an unexpired EAD issued for post-completion OPT. The student will remain in F-1 status and can continue their OPT using the unexpired EAD.\n\nThe student also needs to make sure that USCIS receives a withdrawal request from the petitioner before the H-1B change of status goes into effect. This will prevent USCIS from changing the student’s status to H-1B. Once the petition has been revoked or withdrawn, the student must provide their DSO with a copy of the USCIS acknowledgement of withdrawal (the notice of revocation). The DSO may then contact the SEVIS Help Desk to request a data fix in SEVIS to prevent the student from being terminated in SEVIS.\n\nIf USCIS does not receive the withdrawal request before the date that the student is supposed to change status to an H-1B nonimmigrant, then the student will need to stop working, file Form I-539, Application to Extend/Change Nonimmigrant Status, to request F-1 status, and wait until the change of status request is approved before resuming OPT employment.\n\nThe F-1 student can continue working with their approved EAD while the data fix in SEVIS is pending if:\n\n- The (former) H-1B employer withdrew the H-1B petition before the effective date of the H-1B change of status;\n- The student finds employment appropriate to their OPT;\n- The period of OPT is unexpired (which would indicate that the student was not actually utilizing “cap-gap” since they otherwise had valid OPT authorization); and\n- The DSO has requested a data fix in SEVIS."
    },
    {
      "chunk_id": 516,
      "source_url": "https://studyinthestates.dhs.gov/students/complete/h-1b-status-and-the-cap-gap-extension",
      "section_heading": "H-1B Status and the Cap Gap Extension",
      "content": "H-1B Status and the Cap Gap Extension\n\nLast updated: April 28, 2025\n\nThe H-1B status is temporary employment authorization for a nonimmigrant who performs services in a specialty occupation. An employer may petition [United States Citizenship and Immigration Services (USCIS)](https://www.uscis.gov/) for H-1B status on behalf of an employee/prospective employee if the candidate holds “theoretical or technical expertise in specialized fields.\" USCIS is the government agency responsible for adjudicating H-1B petitions and granting H-1B status.\n\nThere is a limit, or “cap,” on the number of individuals who can receive H-1B status every fiscal year. For purposes of the cap, each fiscal year begins on Oct. 1 of the prior calendar year. For more information on the H-1B cap, visit USCIS’s [H-1B Fiscal Year (FY) 2018 Cap Season](https://www.uscis.gov/working-united-states/temporary-workers/h-1b-specialty-occupations-and-fashion-models/h-1b-fiscal-year-fy-2018-cap-season#how) page.\n\nRegulations prohibit employers from filing H-1B petitions until six months before the date of actual need for the employee. This means that once USCIS reaches the cap in one fiscal year, employers must wait until April 1, which is six months before the start of the next fiscal year, before filing H-1B petitions again."
    }
  ],
  "disclaimer": "This is an unofficial tool. It is not legal advice and is not affiliated with USCIS or DHS. For guidance on your own situation, talk to your DSO or a licensed immigration attorney.",
  "generated_at": "2026-09-06T05:43:15.289431Z",
  "response_type": "refusal_advice",
  "refusal_reason": "query_asks_for_personal_advice",
  "freshness": {
    "as_of": "2026-09-06",
    "sources": [
      {
        "source_url": "https://www.uscis.gov/working-in-the-united-states/temporary-workers/h-1b-specialty-occupations/extension-of-post-completion-optional-practical-training-opt-and-f-1-status-for-eligible-students",
        "page_last_updated": "2025-01-17",
        "last_verified_at": "2026-09-06T03:26:34.945049Z",
        "fetched_at": "2026-08-29T02:01:42.661266Z",
        "rule_effective_date": null
      },
      {
        "source_url": "https://studyinthestates.dhs.gov/students/complete/h-1b-status-and-the-cap-gap-extension",
        "page_last_updated": "2025-04-28",
        "last_verified_at": "2026-09-06T03:26:35.246771Z",
        "fetched_at": "2026-08-29T02:01:43.236499Z",
        "rule_effective_date": null
      }
    ],
    "notices": []
  }
};

export const MOCK_CLARIFY: AnswerResponse = {
  "answer": "Could you say a bit more about what you're asking -- for example, which visa or status (F-1, OPT, STEM OPT, H-1B), and what part of it (a form, a deadline, or a specific rule)?",
  "citations": [],
  "contexts": [],
  "disclaimer": "This is an unofficial tool. It is not legal advice and is not affiliated with USCIS or DHS. For guidance on your own situation, talk to your DSO or a licensed immigration attorney.",
  "generated_at": "2026-09-06T05:43:56.616572Z",
  "response_type": "clarify",
  "refusal_reason": "query_too_vague",
  "freshness": null
};

export const MOCK_NO_ANSWER: AnswerResponse = {
  "answer": "I don't see this covered in my sources, so I'm not going to guess at an answer. Try rephrasing the question, or check with your DSO or a licensed immigration attorney.",
  "citations": [],
  "contexts": [],
  "disclaimer": "This is an unofficial tool. It is not legal advice and is not affiliated with USCIS or DHS. For guidance on your own situation, talk to your DSO or a licensed immigration attorney.",
  "generated_at": "2026-09-06T05:43:51.494503Z",
  "response_type": "no_answer",
  "refusal_reason": "min_distance_exceeds_threshold",
  "freshness": null
};

export const MOCK_BLOCKED_UNVERIFIED: AnswerResponse = {
  "answer": "I generated an answer to this, but it did not pass this tool's citation check, so I'm not showing it. Please try rephrasing the question.",
  "citations": [
    {
      "source_url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt",
      "chunk_id": 441,
      "snippet": "Optional Practical Training Extension for STEM Students (STEM OPT) > Eligibility for the STEM OPT Extension To qualify for the 24-month extension, you must: - Have been granted OPT and currently be in a valid period of post-completion OPT;..."
    },
    {
      "source_url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt",
      "chunk_id": 442,
      "snippet": "Optional Practical Training Extension for STEM Students (STEM OPT) > Applying for a STEM OPT Extension To apply for an extension, you must properly file: - Form I-765 with - The correct application fee; - Your employer’s name as listed in..."
    },
    {
      "source_url": "https://www.uscis.gov/working-in-the-united-states/temporary-workers/h-1b-specialty-occupations/extension-of-post-completion-optional-practical-training-opt-and-f-1-status-for-eligible-students",
      "chunk_id": 511,
      "snippet": "Extension of Post Completion Optional Practical Training (OPT) and F-1 Status for Eligible Students under the H-1B Cap-Gap Regulations > STEM OPT Extensions F-1 students who receive science, technology, engineering, and mathematics (STEM)..."
    },
    {
      "source_url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt",
      "chunk_id": 443,
      "snippet": "Optional Practical Training Extension for STEM Students (STEM OPT) > After Receiving a STEM OPT Extension **Student Reporting Responsibilities** If you receive a STEM OPT extension, you must: - Report changes to the following information..."
    },
    {
      "source_url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-opt-for-f-1-students",
      "chunk_id": 435,
      "snippet": "Optional Practical Training (OPT) for F-1 Students > STEM OPT Extension If you have earned a degree in certain Science, Technology, Engineering and Mathematics (STEM) fields, you may apply for a 24-month extension of your post-completion..."
    }
  ],
  "contexts": [
    {
      "chunk_id": 441,
      "source_url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt",
      "section_heading": "Eligibility for the STEM OPT Extension",
      "content": "Optional Practical Training Extension for STEM Students (STEM OPT) > Eligibility for the STEM OPT Extension\n\nTo qualify for the 24-month extension, you must:\n\n- Have been granted OPT and currently be in a valid period of post-completion OPT;\n- Have earned a bachelor’s, master’s, or doctoral degree from a school that is accredited by a U.S. Department of Education-recognized accrediting agency and is certified by [the Student and Exchange Visitor Program (SEVP)](https://studyinthestates.dhs.gov/school-search) at the time you submit your STEM OPT extension application.\n  - **NOTE: Previously obtained STEM degrees**: If you are an F-1 student participating in a 12-month period of post-completion OPT based on a non-STEM degree, you may be eligible to use a previous STEM degree from a U.S. institution of higher education to apply for a STEM OPT extension. You must have received both degrees from currently accredited and SEVP-certified institutions, and cannot have already received a STEM OPT extension based on this previous degree. The practical training opportunity also must be directly related to the previously obtained STEM degree.\n    - For example, if the student is currently participating in OPT based on a non-STEM  degree, but previously received a bachelor’s degree in a degree program  that appears on the current [DHS STEM Designated Degree Program List](https://www.ice.gov/sevis/schools#dhs-stem-designated-degree-program-list-and-cip-code-nomination-process), the student may be able to apply for a STEM OPT extension based on the bachelor’s degree as long as it is from an accredited U.S. college or university and the OPT employment opportunity is directly related to the bachelor’s STEM degree.\n  - **NOTE: STEM degrees you obtain in the future**: If you enroll in a new academic program in the future and earn another qualifying STEM degree at a higher educational level, you may be eligible for one additional 24-month STEM OPT extension.\n    - For example: If you receive a 24-month STEM OPT extension based on a qualifying bachelor’s degree  and you later earn a qualifying master’s degree, you may apply for an additional 24-month STEM OPT extension based on your master’s degree.\n- Work for an employer who meets all the requirements listed below in the STEM OPT Employer Responsibilities section; and\n- Submit the [Form I-765, Application for Employment Authorization](/i-765), up to 90 days before your current OPT employment authorization expires, and within 60 days of the date your designated school official (DSO) enters the recommendation for OPT into your Student and Exchange Visitor Information System (SEVIS) record."
    },
    {
      "chunk_id": 442,
      "source_url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt",
      "section_heading": "Applying for a STEM OPT Extension",
      "content": "Optional Practical Training Extension for STEM Students (STEM OPT) > Applying for a STEM OPT Extension\n\nTo apply for an extension, you must properly file:\n\n- Form I-765 with\n  - The correct application fee;\n  - Your employer’s name as listed in E-Verify, and\n  - Your employer’s E-Verify Company Identification Number or valid E-Verify Client Company Identification Number\n- Form I-20, Certificate of Eligibility for Nonimmigrant Student Status, endorsed by your DSO within the last 60 days; and\n- A copy of your STEM degree.\n\nIf you file your STEM OPT extension application on time and your OPT period expires while your extension application is pending, we will automatically extend your employment authorization for 180 days. This automatic 180-day extension ceases once USCIS adjudicates your STEM OPT extension application."
    },
    {
      "chunk_id": 511,
      "source_url": "https://www.uscis.gov/working-in-the-united-states/temporary-workers/h-1b-specialty-occupations/extension-of-post-completion-optional-practical-training-opt-and-f-1-status-for-eligible-students",
      "section_heading": "STEM OPT Extensions",
      "content": "Extension of Post Completion Optional Practical Training (OPT) and F-1 Status for Eligible Students under the H-1B Cap-Gap Regulations > STEM OPT Extensions\n\nF-1 students who receive science, technology, engineering, and mathematics (STEM) degrees included on the [STEM Designated Degree Program List (PDF)](https://www.ice.gov/sites/default/files/documents/Document/2016/stem-list.pdf), are employed by employers enrolled in and maintain good standing with E-Verify, and who have received an initial grant of post-completion OPT employment authorization related to such a degree, may apply for a 24-month extension of such authorization. F-1 students may obtain additional information about STEM OPT extensions in the [USCIS Policy Manual](/policy-manual/volume-2-part-f), on our [Optional Practical Training Extension for STEM Students (STEM OPT)](/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt) page, or the [STEM OPT Hub](https://studyinthestates.dhs.gov/stem-opt-hub).\n\nStudents who are eligible for a cap-gap extension of post-completion OPT employment and F-1 status may apply for a STEM OPT extension during the cap-gap extension period.\n\nHowever, students may not apply for a STEM OPT extension once the cap-gap extension period is terminated (if the H-1B petition is rejected, denied, revoked, or withdrawn) and the student has entered the 60-day grace period."
    },
    {
      "chunk_id": 443,
      "source_url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt",
      "section_heading": "After Receiving a STEM OPT Extension",
      "content": "Optional Practical Training Extension for STEM Students (STEM OPT) > After Receiving a STEM OPT Extension\n\n**Student Reporting Responsibilities**\n\nIf you receive a STEM OPT extension, you must:\n\n- Report changes to the following information to your DSO within 10 days of the change, specifically:\n  - Your legal name;\n  - Your residential or mailing address;\n  - Your email address;\n  - Your employer’s name; and\n  - Your employer’s address.\n- Report to your DSO every 6 months to confirm the information listed above, even if none of your information has changed.\n\nFor more information, please refer to the [USCIS Policy Manual](https://www.uscis.gov/policy-manual/volume-2-part-f) and the [DHS STEM OPT Hub](https://studyinthestates.dhs.gov/stem-opt-hub).\n\n**Unemployment during the OPT Period**\n\nYou may be unemployed during your OPT period for a limited number of days.\n\n| If you received… | You may be unemployed for… | For a total of…(during the OPT period) |\n| --- | --- | --- |\n| Initial post-completion OPT only | Up to 90 days | 90 days |\n| 24-month extension | An additional 60 days | 150 days |"
    },
    {
      "chunk_id": 435,
      "source_url": "https://www.uscis.gov/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-opt-for-f-1-students",
      "section_heading": "STEM OPT Extension",
      "content": "Optional Practical Training (OPT) for F-1 Students > STEM OPT Extension\n\nIf you have earned a degree in certain Science, Technology, Engineering and Mathematics (STEM) fields, you may apply for a 24-month extension of your post-completion OPT employment authorization if you:\n\n- Are an F-1 student who received a STEM degree included on the [STEM Designated Degree Program List (PDF)](https://www.ice.gov/sites/default/files/documents/Document/2016/stem-list.pdf);\n- Are employed by an employer who is enrolled in and is using E-Verify; and\n- Received an initial grant of post-completion OPT employment authorization based on your STEM degree.\n\nIf you are interested in applying for a STEM OPT extension, please see our [Optional Practical Training Extension for STEM Students (STEM OPT)](/working-in-the-united-states/students-and-exchange-visitors/optional-practical-training-extension-for-stem-students-stem-opt) page for more information."
    }
  ],
  "disclaimer": "This is an unofficial tool. It is not legal advice and is not affiliated with USCIS or DHS. For guidance on your own situation, talk to your DSO or a licensed immigration attorney.",
  "generated_at": "2026-09-06T05:45:45.653833Z",
  "response_type": "blocked_unverified",
  "refusal_reason": "citation_index_out_of_range",
  "freshness": null
};

export const MOCK_RESPONSES: Record<string, AnswerResponse> = {
  answer: MOCK_ANSWER,
  refusal_advice: MOCK_REFUSAL_ADVICE,
  clarify: MOCK_CLARIFY,
  no_answer: MOCK_NO_ANSWER,
  blocked_unverified: MOCK_BLOCKED_UNVERIFIED,
};

// The real question asked to produce each fixture above, so ?mock=<state> can echo it the same
// way a real turn would.
export const MOCK_QUESTIONS: Record<string, string> = {
  answer: "How long is the STEM OPT extension?",
  refusal_advice: "My OPT ends in July but my H-1B starts in October. Am I covered?",
  clarify: "help",
  no_answer: "How do I make sourdough bread rise properly?",
  blocked_unverified: "How long is the STEM OPT extension?",
};
