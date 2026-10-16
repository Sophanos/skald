"""Commander additions use model meaning and one reversible household preview."""
import json
from pathlib import Path
import textwrap
from unittest.mock import patch

from decide_kit import ReplayLLM, reset
from test_w92_commands import Household
from skald import commands as c, setup


def member(label, **fields):
    return dict(op='member_create', kind=None, values=[], person=label.casefold(), label=label, guest=False, **fields)


def plan(*steps):
    return dict(op='plan', kind=None, values=[], person=None, steps=list(steps))


class People(Household):
    def test_command_contract_invalidates_older_model_answers(self):
        self.assertEqual(self.s.DECISIONS['parse_command']['ver'], 20)

    def test_multi_person_undo_keeps_later_budget_and_lists_both_changes(self):
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=plan(
            member('Mia', role='child', age=9), member('Jonas', role='adult'))))
        people_id = c.changes(self.s, 'ana')['changes'][0]['id']
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=dict(op='ai_budget', values=[5.5], currency='EUR')))
        history = c.preview(self.s, '', 'ana', answer=dict(op='history', person='household', since='7d'))
        self.assertEqual(len(history['changes']), 2)
        self.assertTrue(history['read_only'])
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=dict(op='undo', values=[people_id])))
        self.assertNotIn('mia', self.s.PEOPLE)
        self.assertNotIn('jonas', self.s.PEOPLE)
        self.assertEqual(self.s.ai_monthly_usd(), 5.5)

    def model_say(self, text, answer, routed=True, results=None):
        with ReplayLLM(self.s) as llm:
            llm.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
            result = c.special(self.s, text, 'movie', routed=routed, results=results)
            self.assertEqual(llm.calls, 1)
            return result['metas'][0], llm.bodies[0]['messages'][1]['content']

    def files(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()
                and p.name not in ('usage.json', 'usage.lock', 'history.lock', 'decisions.jsonl', 'decide_cache.json')}

    def test_relationship_and_bare_names_keep_the_resolved_label(self):
        for text, label in [('Add my new flatmate Jonas', 'Jonas'), ('My girlfriend Lena is moving in', 'Lena'),
                            ('Add my mother Eva', 'Eva'), ('Meine Mutter Eva wohnt jetzt bei uns', 'Eva'),
                            ('Dodaj mamu Evu', 'Eva'), ('Add Eva', 'Eva'),
                            ('Füge Jonas hinzu', 'Jonas'), ('Dodaj Jonasa', 'Jonas')]:
            with self.subTest(text=text):
                before = c.config(self.s)
                card, _ = self.model_say(text, member(label))
                pending = c.get_pending(self.s, 'w92')
                self.assertEqual(pending['member']['config_after']['household']['labels'][label.casefold()], label)
                self.assertNotIn(label.casefold(), pending['member']['config_after']['household']['guests'])
                self.assertIn('yes ', card['description'])
                self.assertEqual(c.config(self.s), before)

    def test_guest_comes_from_model_meaning(self):
        for text, guest in [('My mother Eva visits for a week', True), ('Lena is staying a few days', True),
                            ('Jonas only visits sometimes', True), ('My friend Jonas is over for the weekend', True),
                            ('My mother Eva lives with us now', False), ('My partner Lena is moving in', False)]:
            with self.subTest(text=text):
                label = 'Eva' if 'Eva' in text else 'Lena' if 'Lena' in text else 'Jonas'
                answer = {**member(label), 'guest': guest}
                pending = c.preview(self.s, text, 'ana', answer=answer)
                self.assertEqual(label.casefold() in pending['member']['config_after']['household']['guests'], guest)

    def test_unnamed_relationship_asks_one_short_name_question(self):
        before = self.files()
        card, _ = self.model_say('Add my mother', dict(op='ask', slot='person', new_person=True,
                                                     question='What should we call her?'))
        self.assertEqual(card['name'], 'What should we call her?')
        self.assertEqual(card['description'], 'Reply with that detail.')
        self.assertTrue(c.get_pending(self.s, 'w92')['ask'])
        self.assertEqual(self.files(), before)

    def test_name_reply_becomes_label_including_an_explicit_role_word(self):
        for label, text in (('Eva', 'Add my mother'), ('Mama', 'Add my girlfriend')):
            with self.subTest(label=label):
                self.model_say(text, dict(op='ask', slot='person', new_person=True,
                                                   question='What should we call her?'))
                _, context = self.model_say(label, member(label), routed=False)
                self.assertIn(text, context)
                self.assertIn('What should we call her?', context)
                self.assertEqual(c.get_pending(self.s, 'w92')['change']['label'], label)
                self.confirm()
                self.assertEqual(c.config(self.s)['household']['labels'][label.casefold()], label)
                self.say('undo')

    def test_child_without_age_asks_then_keeps_the_reply_age(self):
        before = self.files()
        card, _ = self.model_say('Add my son Leo', dict(op='ask', slot='age', new_person=True,
                                                      question='How old is Leo?'))
        self.assertEqual(card['name'], 'How old is Leo?')
        self.assertEqual(self.files(), before)
        _, context = self.model_say('7', member('Leo', age=7), routed=False)
        self.assertIn('Add my son Leo', context)
        self.confirm()
        self.assertEqual(self.s.kids_age('leo'), 7)

    def test_recent_search_name_reply_uses_pending_question(self):
        recent = dict(query='Dune', rows={'movie': [dict(key='tmdb:990001', kind='movie', title='Dune')]})
        self.assertIsNone(c.special(self.s, 'Dune', 'movie'))
        for routed in (False, True):
            with self.subTest(routed=routed):
                reset(self.s)
                c.pending(self.s).clear()
                self.model_say('Add my mother', dict(op='ask', slot='person', new_person=True,
                                                    question='What should we call her?'), results=recent)
                _, context = self.model_say('Eva', member('Eva'), routed=routed, results=recent)
                self.assertIn('Add my mother', context)
                self.assertIn('What should we call her?', context)
                self.assertNotIn('Dune', context)
                self.assertEqual(c.get_pending(self.s, 'w92')['change']['label'], 'Eva')

    def test_command_input_prefers_pending_question_to_recent_results(self):
        from skald.rowplan import command_input
        self.model_say('Add my mother', dict(op='ask', slot='person', new_person=True,
                                            question='What should we call her?'))
        inp = command_input(self.s, 'Eva', dict(query='Dune', rows={}),
                            viewer='ana', prior=c.get_pending(self.s, 'w92'))
        self.assertEqual(inp['last'], 'Add my mother\nQuestion: What should we call her?')
        self.assertNotIn('shown', inp)

    def test_title_search_interrupts_member_clarification(self):
        self.model_say('Add my mother', dict(op='ask', slot='person', new_person=True,
                                            question='What should we call her?'))
        before = self.files()
        with ReplayLLM(self.s) as llm:
            llm.arm({'choices': [{'message': {'content': json.dumps(c.NONE)}}]})
            result = c.special(self.s, 'Find the film Arrival', 'movie')
            self.assertEqual(llm.calls, 1)
        self.assertIsNone(result)
        self.assertIsNone(c.get_pending(self.s, 'w92'))
        self.assertEqual(self.files(), before)

    def test_child_age_reply_preserves_an_earlier_name_reply(self):
        self.model_say('Add my child', dict(op='ask', slot='person', new_person=True,
                                          question='What should we call them?'))
        self.model_say('Mia', dict(op='ask', slot='age', new_person=True, question='How old is Mia?'), routed=False)
        _, context = self.model_say('9', member('Mia', age=9), routed=False)
        self.assertIn('Add my child', context)
        self.assertIn('Mia', context)
        self.confirm()
        self.assertEqual(self.s.kids_age('mia'), 9)

    def test_api_name_clarification_keeps_context(self):
        req = dict(viewer='ana', client={'name': 'ana'}, body={'in': {'query': 'Add my girlfriend'}})
        answers = [dict(op='ask', slot='person', new_person=True, question='What should we call her?'), member('Lena')]
        with patch.object(self.s, 'decide', side_effect=[{'answer': a} for a in answers]) as model:
            first = c.api_preview(self.s, req)
            self.assertTrue(first['preview']['read_only'])
            req['body']['in']['query'] = 'Lena'
            second = c.api_preview(self.s, req)
        self.assertIn('Add my girlfriend', model.call_args.args[1]['last'])
        self.assertEqual(second['preview']['change']['label'], 'Lena')

    def test_an_api_clarification_cannot_be_confirmed_as_a_change(self):
        req = dict(viewer='ana', client={'name': 'ana'}, body={'in': {'query': 'Add my mother'}})
        with patch.object(self.s, 'decide', return_value={'answer': dict(op='ask', slot='person', new_person=True,
                                                                       question='What should we call her?')}):
            c.api_preview(self.s, req)
        before = self.files()
        req['body'] = {'plan': '0000000000000000'}
        with self.assertRaisesRegex(ValueError, 'Preview again'):
            c.api_apply(self.s, req)
        self.assertEqual(self.files(), before)

    def test_other_read_only_questions_do_not_become_member_replies(self):
        self.model_say('Make her recommendations gentler', dict(op='ask', slot='person', question='Which person?'))
        self.assertIsNone(c.get_pending(self.s, 'w92'))
        with patch.object(c, 'preview', return_value=dict(read_only=True, ask=True, people=[], name='Which row?', description='Which row?')):
            c.special(self.s, 'Explain a synthetic lab row', 'movie', routed=True)
        self.assertIsNone(c.get_pending(self.s, 'w92'))

    def test_child_and_multi_person_steps_apply_and_undo_once(self):
        for answer in (member('Leo', age=7), plan(member('Mia', age=9), member('Leo', age=4)),
                       plan(member('Jonas'), {**member('Lena'), 'guest': True}, member('Mia', age=9))):
            with self.subTest(answer=answer):
                before = c.config(self.s)
                text = 'Add '+', '.join(x['label'] for x in answer.get('steps', [answer]))
                self.model_say(text, answer)
                pending = c.get_pending(self.s, 'w92')
                steps = answer.get('steps', [answer])
                if len(steps) > 1:
                    self.assertEqual([x for x, _ in pending['steps']],
                                     [{**dict.fromkeys(c.OUT.keys() - {'since', 'currency'}), **x} for x in steps])
                self.confirm()
                cfg = c.config(self.s)
                for step in steps:
                    who = step['person']
                    self.assertEqual(cfg['household']['labels'][who], step['label'])
                    self.assertEqual(who in cfg['household']['guests'], step['guest'])
                    self.assertEqual(self.s.kids_age(who), step.get('age'))
                self.assertEqual(sum(not e.get('undone') for e in c.journal(self.s)['ana']), 1)
                self.say('undo')
                self.assertEqual(c.config(self.s), before)

    def test_missing_detail_in_a_step_asks_without_a_partial_preview(self):
        before = self.files()
        answer = plan(member('Mia', age=9), dict(op='ask', kind=None, values=[], person=None,
                                               slot='age', new_person=True, question='How old is Leo?'))
        pending = c.preview(self.s, 'Add my kids Mia 9 and Leo', 'ana', answer=answer)
        self.assertTrue(pending['read_only'])
        self.assertIn('How old is Leo?', pending['name'])
        c.apply(self.s, pending)
        self.assertEqual(self.files(), before)

    def test_child_role_without_age_never_applies_and_keeps_other_people(self):
        before = self.files()
        answer = plan(member('Mia', role='child', age=9), member('Leo', role='child'))
        pending = c.preview(self.s, 'Add my kids Mia 9 and Leo', 'ana', answer=answer)
        self.assertTrue(pending['read_only'])
        self.assertTrue(pending['ask'])
        self.assertEqual(pending['name'], 'How old is Leo?')
        c.apply(self.s, pending)
        self.assertEqual(self.files(), before)
        c.pending(self.s)['w92'] = (self.s.time.time(), pending)
        _, context = self.model_say('4', plan(member('Mia', role='child', age=9),
                                            member('Leo', role='child', age=4)), routed=False)
        self.assertIn('Mia', context)
        self.assertIn('9', context)
        self.assertIn('How old is Leo?', context)
        self.confirm()
        self.assertEqual(self.s.kids_age('mia'), 9)
        self.assertEqual(self.s.kids_age('leo'), 4)

    def test_single_child_role_without_age_asks_before_applying(self):
        before = self.files()
        pending = c.preview(self.s, 'Add Leo', 'ana', answer=member('Leo', role='child'))
        self.assertTrue(pending['ask'])
        self.assertEqual(pending['name'], 'How old is Leo?')
        c.apply(self.s, pending)
        self.assertEqual(self.files(), before)

    def test_invalid_or_duplicate_second_person_writes_nothing(self):
        before = self.files()
        for second in (member('Jonas'), member('Leo', age=2)):
            with self.subTest(second=second), self.assertRaises(ValueError):
                c.preview(self.s, 'Add Jonas and Leo', 'ana', answer=plan(member('Jonas'), second))
        self.assertEqual(self.files(), before)

    def test_six_person_cap_is_one_clear_message_and_no_writes(self):
        for label in ('Eva', 'Mia', 'Leo'):
            c.apply(self.s, c.preview(self.s, '', 'ana', answer=member(label)))
        before = self.files()
        card, _ = self.model_say('Add Jonas and Lena', plan(member('Jonas'), member('Lena')))
        self.assertIn('at most 6 people', card['description'])
        self.assertIsNone(c.get_pending(self.s, 'w92'))
        self.assertEqual(self.files(), before)
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=member('Jonas')))
        before = self.files()
        card, _ = self.model_say('Add Lena', member('Lena'))
        self.assertIn('at most 6 people', card['description'])
        self.assertEqual(self.files(), before)

    def test_apply_rechecks_capacity_for_the_whole_plan(self):
        for label in ('Eva', 'Mia'):
            c.apply(self.s, c.preview(self.s, '', 'ana', answer=member(label)))
        pending = c.preview(self.s, '', 'ana', answer=plan(member('Jonas'), member('Lena')))
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=member('Leo')))
        before = self.files()
        with self.assertRaisesRegex(ValueError, 'limit.*6'):
            c.apply(self.s, pending)
        self.assertEqual(self.files(), before)

    def test_undo_checks_evidence_for_every_added_person(self):
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=plan(member('Jonas'), member('Lena'))))
        self.s.write_feedback(dict(who='lena', key='tmdb:990001', kind='movie', title='Invented Meadow', action='love'))
        before = self.files()
        with self.assertRaisesRegex(ValueError, 'Lena has data'):
            c.undo(self.s, ['ana'])
        self.assertEqual(self.files(), before)

    def test_rollback_undo_checks_evidence_for_every_added_person(self):
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=plan(member('Jonas'), member('Lena'))))
        self.s.write_feedback(dict(who='lena', key='tmdb:990001', kind='movie', title='Invented Meadow', action='love'))
        before = self.files()
        # Copy cc016e7's member undo rule; apply the shipped rollback patch, never load old code from git.
        rule = textwrap.dedent('''\
            if m['name'] != 'pair' and not m['before']['member']:
                from .setup import member_data
                rest = copy.deepcopy(cfg)
                rest['taste']['people'].pop(m['name'], None)
                if member_data(Path(s.DATA), m['name'], rest, skip=('commands.json',)):
                    raise ValueError(m['name'].title()+' has data; use skald members remove to keep their evidence')
            if 'config_before' in m:
                cfg = s.validate_config(inverse_prefs(cfg, m['config_before'], m['config_after']), data=s.DATA)
            elif member_state(cfg, m['name']) != m['after']:
                raise ValueError('Members changed; cannot undo this command')
            if 'config_before' not in m:
                cfg = s.validate_config(set_member(cfg, m['name'], m['before']), data=s.DATA)
            return cfg
        ''')
        path = Path(__file__).resolve().parents[1] / 'ops/rollback-1.4.9.patch'
        if path.exists():
            lines = path.read_text().splitlines(keepends=True)[3:]
            old = textwrap.dedent(''.join(line[1:] for line in lines if line.startswith((' ', '-'))))
            new = textwrap.dedent(''.join(line[1:] for line in lines if line.startswith((' ', '+'))))
            self.assertIn(old, rule)
            rule = rule.replace(old, new)
        namespace = dict(vars(c))
        exec('def rollback_undo(s, cfg, m):\n'+textwrap.indent(rule, '    '), namespace)
        entry = c.journal(self.s)['ana'][-1]
        with self.assertRaisesRegex(ValueError, 'Lena has data'):
            namespace['rollback_undo'](self.s, c.config(self.s), entry['member'])
        self.assertEqual(self.files(), before)

    def test_pronouns_and_unnamed_roles_are_clarifications(self):
        for text in ('Add her', 'Füge sie hinzu', 'Dodaj nju', 'Add my girlfriend', 'Dodaj mamu'):
            with self.subTest(text=text):
                before = self.files()
                self.model_say(text, dict(op='ask', slot='person', new_person=True, question='What should we call them?'))
                self.assertEqual(self.files(), before)
        pending = c.preview(self.s, 'Add her', 'ana', answer=dict(op='member_create', person='her', label='Lena'))
        self.assertTrue(pending['read_only'])

    def test_kids_and_guests_still_cannot_add_any_people(self):
        cfg = c.config(self.s)
        cfg['taste']['people']['ben']['prefs'] = {'kids_age': 9}
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        for viewer in ('ben', self.s.GUEST):
            for answer in (member('Jonas'), plan(member('Jonas'), member('Lena'))):
                with self.subTest(viewer=viewer, answer=answer), self.assertRaises(PermissionError):
                    c.preview(self.s, '', viewer, answer=answer)
        c.apply(self.s, c.preview(self.s, '', 'ana', answer={**member('Eva'), 'guest': True}))
        for answer in (member('Jonas'), plan(member('Jonas'), member('Lena'))):
            with self.subTest(answer=answer), self.assertRaises(PermissionError):
                c.preview(self.s, '', 'eva', answer=answer)

    def test_apply_rechecks_the_actor_for_a_multi_person_plan(self):
        pending = c.preview(self.s, '', 'ana', answer=plan(member('Jonas'), member('Lena')))
        c.apply(self.s, c.preview(self.s, '', 'ben', answer=dict(op='member_update', person='ana', guest=True)))
        before = self.files()
        with self.assertRaises(PermissionError):
            c.apply(self.s, pending)
        self.assertEqual(self.files(), before)
