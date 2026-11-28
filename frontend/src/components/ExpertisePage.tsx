import { useEffect, useState, type FormEvent } from "react";
import { api, describeError } from "../api";
import { IconSearch, Spinner } from "../icons";
import { useFeedback } from "./Feedback";
import type { ExpertiseMatch } from "../types";
import { Alert, PageHeader } from "./ui";

export function ExpertisePage() {
  const { toast } = useFeedback();
  const [tagsText, setTagsText] = useState("");
  const [profileLoading, setProfileLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [searching, setSearching] = useState(false);
  const [skill, setSkill] = useState("");
  const [searchedSkill, setSearchedSkill] = useState("");
  const [matches, setMatches] = useState<ExpertiseMatch[] | null>(null);
  const [error, setError] = useState("");
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  useEffect(() => {
    void api.myExpertise()
      .then((profile) => setTagsText(profile.tags.join(", ")))
      .catch((err) => setError(describeError(err)))
      .finally(() => setProfileLoading(false));
  }, []);

  async function saveProfile(event: FormEvent) {
    event.preventDefault();
    const tags = tagsText.split(/[\n,]/).map((tag) => tag.trim()).filter(Boolean);
    setSaving(true);
    setError("");
    try {
      const profile = await api.updateMyExpertise(tags);
      setTagsText(profile.tags.join(", "));
      toast("success", "Expertise profile saved", "Your skills remain editable and can be cleared at any time.");
    } catch (err) {
      const message = describeError(err);
      setError(message);
      toast("error", "Could not save expertise", message);
    } finally {
      setSaving(false);
    }
  }

  async function searchDirectory(event: FormEvent) {
    event.preventDefault();
    setSearching(true);
    setError("");
    try {
      const result = await api.searchExpertise(skill.trim());
      setSearchedSkill(result.skill);
      setMatches(result.matches);
      setNextCursor(result.next_cursor);
    } catch (err) {
      const message = describeError(err);
      setError(message);
      setMatches(null);
      setNextCursor(null);
    } finally {
      setSearching(false);
    }
  }

  async function loadMore() {
    if (!nextCursor) return;
    setSearching(true);
    setError("");
    try {
      const result = await api.searchExpertise(searchedSkill, nextCursor);
      setMatches((current) => [...(current ?? []), ...result.matches]);
      setNextCursor(result.next_cursor);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSearching(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Expertise directory"
        description="Find colleagues who have chosen to share a skill with your department."
      />
      {error && <Alert tone="danger" className="mb-4">{error}</Alert>}
      <section className="card expertise-card">
        <div className="card-title">
          <div><h2>Your expertise</h2><p className="muted">Add up to 20 short skill tags, separated by commas or new lines.</p></div>
        </div>
        <form className="expertise-form" onSubmit={saveProfile}>
          <label className="field">
            <span className="field-label">Skills you choose to share</span>
            <textarea
              aria-label="Skills you choose to share"
              maxLength={1400}
              rows={3}
              value={tagsText}
              disabled={profileLoading || saving}
              onChange={(event) => setTagsText(event.target.value)}
              placeholder="e.g. Python, financial modeling, safety procedures"
            />
          </label>
          <div className="expertise-form-footer">
            <small className="muted text-xs">Only people sharing a department with you can find your tags. Avoid sensitive personal information.</small>
            <button className="btn btn-primary" disabled={profileLoading || saving}>
              {saving ? "Saving…" : "Save my skills"}
            </button>
          </div>
        </form>
      </section>
      <section className="card expertise-card">
        <div className="card-title">
          <div><h2>Find a colleague</h2><p className="muted">Search uses an exact skill tag. Results update immediately when department access changes.</p></div>
        </div>
        <form className="expertise-search" onSubmit={searchDirectory}>
          <label className="field expertise-search-field">
            <span className="field-label">Skill tag</span>
            <input
              type="search"
              aria-label="Skill tag"
              minLength={2}
              maxLength={60}
              required
              value={skill}
              onChange={(event) => setSkill(event.target.value)}
              placeholder="e.g. python"
            />
          </label>
          <button className="btn btn-primary" disabled={searching}>
            {searching ? <Spinner size={15} /> : <IconSearch size={16} />}
            Search
          </button>
        </form>
        {matches !== null && (
          <div className="expertise-results" aria-live="polite">
            <h3>{matches.length ? `People sharing “${searchedSkill}”` : `No matches for “${searchedSkill}”`}</h3>
            {matches.map((match) => (
              <article className="expertise-result" key={match.user_id}>
                <div className="expertise-result-main">
                  <strong>{match.email}</strong>
                  <div className="chips">{match.tags.map((tag) => <span className="chip" key={tag}>{tag}</span>)}</div>
                </div>
                <small className="muted text-xs">Shared in {match.shared_departments.join(", ") || "your organization"}</small>
              </article>
            ))}
            {nextCursor && <button type="button" className="btn" disabled={searching} onClick={() => void loadMore()}>Load more</button>}
          </div>
        )}
      </section>
    </>
  );
}
