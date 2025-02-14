import difflib
from operator import itemgetter

from counts.models import Election, Candidate, Stage, StageCell
from counts.utils import parse_election_id, get_elections_ni_constituency_count_data, get_alternative_person_id
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from uk_election_ids.election_ids import validate

constituency_fixes = {
    'fermanagh-and-south-tyrone': 'fermanagh-south-tyrone',
    'newry-and-armagh': 'newry-armagh'
}

candidate_last_chance_fixes = {
    'william dickson': 'billy dickson'
}

class Command(BaseCommand):
    help = "Drops and re-synchronises a given election, it's child-ballots, and their candidates against the democracyclub endpoint"

    def add_arguments(self, parser):
        parser.add_argument("election_id", type=str)

    def handle(self, *args, **options):
        election_id = options["election_id"]
        if not validate(election_id):
            raise CommandError(f"Election ID {election_id} cannot be validated")

        if (election := Election.objects.filter(id=election_id).first()) is not None:
            self.stderr.write(f'Purging Existing election: {election_id}')
            election.delete()

        election = Election.objects.create(id=election_id)

        data = election.get_data()
        if "ballots" in data:
            ballots = []
            for ballot in data["ballots"]:
                e, created = Election.objects.get_or_create(
                    id=ballot["ballot_paper_id"], parent=election
                )
                if created:
                    self.stdout.write(
                        f"new election, {e.id} created as part of {election_id}"
                    )
                e.populate_candidates()
                ballot_desc = parse_election_id(e.id)

                if ballot_desc["constituency"] in constituency_fixes:
                    count_data = get_elections_ni_constituency_count_data(
                        year=ballot_desc["date"].year, constituency=constituency_fixes[ballot_desc["constituency"]]
                    )
                else:
                    count_data = get_elections_ni_constituency_count_data(
                        year=ballot_desc["date"].year, constituency=ballot_desc["constituency"]
                    )

                stage = None
                stage_number = 0
                stage_cells = []
                counted_stages = []
                for count_row in count_data:
                    try:
                        if (new_stage := int(count_row["Count_Number"])) != stage_number:
                            counted_stages.append(stage_number)  # for testing monotonicity later
                            stage_number = new_stage
                            stage = Stage.objects.create(
                                count_stage=new_stage, election=e,
                                author=User.objects.get(username='admin'),
                                validated_by=User.objects.get(username='admin')
                            )

                        count = float(count_row["Total_Votes"])
                        if not Candidate.objects.filter(id=int(count_row["Candidate_Id"])).exists():
                            candidate_name = ' '.join([count_row['Firstname'], count_row['Surname']])
                            potential_candidate = sorted([
                                (c, difflib.SequenceMatcher(a=c.name.lower(), b=candidate_name.lower()).ratio())
                                for c in Candidate.objects.filter(standing__in=[e])
                            ], key=itemgetter(1))[-1]
                            if potential_candidate[1] > 0.5:  # reverse sorted ratio
                                ## Close enough for me within the small pool of candidates for this election
                                candidate = potential_candidate[0]
                                self.stdout.write(f'Fixed match of candidate {candidate_name} to {candidate}')
                            else:
                                self.stdout.write(f"Cannot find candidate for {count_row}, checking democracyclub")
                                candidate_id = get_alternative_person_id(int(count_row["Candidate_Id"]))
                                if candidate_id is None:
                                    raise RuntimeError(f"Cannot find candidate for {count_row}")
                                candidate = Candidate.objects.get(id=candidate_id)
                        else:
                            candidate = Candidate.objects.get(id=int(count_row["Candidate_Id"]))

                        StageCell.objects.create(
                            stage=stage, candidate=candidate, count=count
                        )
                    except BaseException as e:
                        self.stderr.write(f'Could not parse count_row: {count_row}')
                        raise RuntimeError(f'Could not parse count_row for {ballot_desc}') from e
