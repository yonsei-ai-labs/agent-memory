"""Export public claim evidence, seal decisions and guesses, then measure masking after unblinding."""

import argparse
from pathlib import Path

from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.analysis.semantic_review import RUBRIC_HASH, judge_messages, rating_agreement, validate_reviews
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.recording.assets import publish_json


def public_bundle(packets):
    public = []
    for key, packet in sorted(packets.items()):
        require(key == packet["packet_id"], "Review case ID changed")
        messages = judge_messages(packet)
        public.append(dict(packet_id=key, input_sha256=digest(canonical(messages)), messages=messages))
    return dict(rubric_sha256=RUBRIC_HASH, packets=public)


def _read_bundle(bundle):
    require(set(bundle) == {"rubric_sha256", "packets"} and bundle["rubric_sha256"] == RUBRIC_HASH,
            "Wrong public review bundle")
    packets = {}
    for row in bundle["packets"]:
        key = row["packet_id"]
        require(key not in packets, "Repeated public review packet")
        evidence = read_json(row["messages"][1]["content"].encode())["evidence"]
        packets[key] = dict(packet_id=key, evidence=evidence)
    require(public_bundle(packets) == bundle, "Public judge input changed")
    return packets


def export_public(directory, packets):
    """Create a fresh reviewer directory containing only the exact judge messages."""
    bundle = public_bundle(packets)
    directory.mkdir(parents=True, exist_ok=False)
    publish_json(directory / "inputs.json", bundle)
    return digest(canonical(bundle))


def _validated_ratings(packets, submission, guesses):
    rated = validate_reviews(packets, submission)
    require(isinstance(guesses, dict) and guesses.keys() == rated.keys()
            and all(g is None or (isinstance(g, str) and g.strip()) for g in guesses.values()),
            "Record a condition guess or an abstention for every rated packet")
    return rated


def seal_review(bundle, submission, guesses):
    """This function has no assignment key. Null records an abstention on condition identity."""
    _validated_ratings(_read_bundle(bundle), submission, guesses)
    material = dict(format="blinded-claim-review/v1", bundle_sha256=digest(canonical(bundle)),
                    submission=submission, condition_guesses=guesses)
    return dict(**material, seal_sha256=digest(canonical(material)))


def _open_seal(packets, bundle_sha, sealed):
    material = {k: v for k, v in sealed.items() if k != "seal_sha256"}
    require(set(material) == {"format", "bundle_sha256", "submission", "condition_guesses"}
            and material["format"] == "blinded-claim-review/v1" and material["bundle_sha256"] == bundle_sha
            and sealed.get("seal_sha256") == digest(canonical(material)), "Review or guesses changed since sealing")
    return _validated_ratings(packets, material["submission"], material["condition_guesses"])


def blinding_check(bundle, sealed, conditions):
    """Analyst-only join after sealing. One public packet can occur in more than one condition."""
    packets = _read_bundle(bundle)
    _open_seal(packets, digest(canonical(bundle)), sealed)
    all_ids = set(packets)
    require(set(conditions) == all_ids and all(isinstance(v, (set, frozenset)) and v
            and all(isinstance(c, str) and c for c in v) for v in conditions.values()), "Incomplete assignment key")
    correct = guessed = ambiguous = abstained = 0
    for key, guess in sealed["condition_guesses"].items():
        if len(conditions[key]) != 1:
            ambiguous += 1
        elif guess is None:
            abstained += 1
        else:
            guessed += 1
            correct += guess in conditions[key]
    return dict(assigned=len(all_ids), rated=len(sealed["condition_guesses"]), shared_conditions=ambiguous,
                abstained=abstained, guessed=guessed, correct=correct, accuracy=correct / guessed if guessed else None)


def compare_sealed_reviews(bundle, first, second):
    """Compute agreement before unblinding, with two identified reviewers and distinct original replies."""
    packets = _read_bundle(bundle)
    bundle_sha = digest(canonical(bundle))
    ratings = []
    response_ids = set()
    for seal in (first, second):
        submission = seal["submission"]
        checked = _open_seal(packets, bundle_sha, seal)
        identities = {(submission["reviewer"].get("provider"), r["response_id"])
                      for r in checked.values() if "response_id" in r}
        require(not response_ids & identities, "The two submissions reuse original model replies")
        response_ids.update(identities)
        ratings.append(checked)
    reviewers = [seal["submission"]["reviewer"]["id"] for seal in (first, second)]
    require(reviewers[0] != reviewers[1], "Two separately identified reviewers are required")
    return dict(unique_packets=len(packets), reviewers=reviewers, reviewed=[len(r) for r in ratings],
                **rating_agreement(*ratings))


def main():
    parser = argparse.ArgumentParser(description="Seal a review using only an exported public bundle")
    for name in ("bundle", "ratings", "guesses", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    sealed = seal_review(*(read_json(path.read_bytes()) for path in (args.bundle, args.ratings, args.guesses)))
    publish_json(args.output, sealed)


if __name__ == "__main__":
    main()
