# Huddle Helper

Records a call on one laptop, shows a live transcript while it runs, and turns it into a final transcript with named speakers after Stop.

## Language

**Final pass**:
Everything that happens to a meeting after Stop to turn its live transcript into the final transcript, ending with the meeting marked done.
_Avoid_: After-Stop pass, finalize, finalizing, post-processing

**Second transcription**:
An optional stage of the final pass that transcribes the kept audio again to improve the wording of the live transcript.
_Avoid_: Retranscribe, re-transcription, improving the transcript (that is its on-screen label, not its name)

**Keep audio**:
A meeting's own choice of whether its recorded audio survives the final pass. It starts as the Keep audio setting and can be changed for that one meeting until the final pass ends.

### Transcripts

**Live transcript**:
The transcript written line by line while a meeting records. It is never rewritten, so it stays as evidence of what was shown live.
_Avoid_: Provisional transcript, draft transcript

**Final transcript**:
The transcript the final pass produces from the whole recording. It is the source of truth once the meeting is done, but it is not verified: it can disagree with the live transcript, and either may be wrong.
_Avoid_: Best transcript, verified transcript, corrected transcript

**Transcript version**:
A stamp that names one particular final transcript of a meeting. It changes whenever that transcript is replaced, so a quote can say exactly which wording it came from.
_Avoid_: Revision (in prose), cursor

**Gap**:
A stretch of a meeting with no audio on one stream, either lost or deliberately muted by the user.
_Avoid_: Dropout, outage

**Bookmark**:
A moment in a meeting the user marked, by its time on the meeting clock, optionally with a type and a label.
_Avoid_: Mark, flag, highlight

### People

**Speaker label**:
The name the app gives a distinct voice it detected, such as `SPEAKER_01`. It says only that the same voice spoke, not who it is.
_Avoid_: Speaker id, voice

**Speaker name**:
A display name the user (or the downstream app) gave a speaker label. It is a label for reading, not a confirmed identity.
_Avoid_: Identity, participant, person

**Shared mic**:
A stretch of a meeting the user marked as having several people speaking into their microphone. Mic speech inside it is separated into speakers; outside it, it is all `You`. On screen it is "Several people on my mic".
_Avoid_: In-room mode, mic diarization

**Attendee**:
A name typed in the Start dialog's "Who's on the call" box to make naming speakers quicker. It is not a record of who attended or who spoke.
_Avoid_: Participant, invitee, recipient

### Meetings

**Active meeting**:
The meeting the recorder is recording right now, confirmed by the recorder still being alive. A meeting that stopped or whose recorder died is never active, however recent.
_Avoid_: Current meeting

**Latest meeting**:
The active meeting if there is one, otherwise the most recently started meeting in whatever state it is in (still in its final pass, done, interrupted or failed). Whoever asks for it is always told which of the two it got.
_Avoid_: Newest meeting, last meeting, most recent meeting

**Downstream app**:
A separate application on the same PC that reads Huddle Helper's meeting files to interpret and act on a meeting. Huddle Helper supplies the conversation; the downstream app owns everything it does with it.
_Avoid_: Consumer, client, integration
