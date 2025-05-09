import os
import time

import numpy as np
import regex as re
from claudette import Chat, models
from evaluate import load
from anthropic import RateLimitError
import regex as re


class Metric:
    def __init__(self, **kwargs):
        self._load_metric(**kwargs)

    def _load_metric(self, **kwargs):
        raise NotImplementedError("This method should be overridden by subclasses.")

    def compute(self, prompts, predictions, references):
        raise NotImplementedError("This method should be overridden by subclasses.")


class Rouge(Metric):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def _load_metric(self, **kwargs):
        self.metric = load("rouge", keep_in_memory=True)

    def compute(self, prompts, predictions, references):
        return self.metric.compute(predictions=predictions, references=references)


class Bleurt(Metric):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def _load_metric(self, **kwargs):
        self.metric = load("bleurt", keep_in_memory=True)

    def compute(self, prompts, predictions, references):
        return np.mean(
            self.metric.compute(predictions=predictions, references=references)[
                "scores"
            ]
        )


class BertScore(Metric):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def _load_metric(self, **kwargs):
        self.metric = load("bertscore", keep_in_memory=True)

    def compute(self, prompts, predictions, references):
        result = self.metric.compute(
            predictions=predictions, references=references, lang="en"
        )
        return {
            "precision": np.mean(result["precision"]),
            "recall": np.mean(result["recall"]),
            "f1": np.mean(result["f1"]),
        }


class Accuracy(Metric):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def _load_metric(self, **kwargs):
        from sklearn.metrics import accuracy_score

        self.metric = accuracy_score

    def compute(self, prompts, predictions, references):
        return self.metric(references, predictions)


class ExactMatchScore(Metric):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def _load_metric(self, **kwargs):
        pass

    def compute(self, prompts, predictions, references):
        answer = []
        for p, r, in zip(predictions, references):
            # print(f"p ==> {p}")
            if type(r) == list:
                r = r[0]
            if p.split() == r.split():
                answer.append(1)
            else:
                answer.append(0)
                
        return np.mean(answer)
        # return np.mean(
        #     [
        #         1 if p.split() == r.split() else 0 
        #         for p, r in zip(predictions, references)
        #     ]
        # )


class LevenshteinDistance(Metric):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def _load_metric(self, **kwargs):
        from fuzzywuzzy import fuzz

        self.metric = fuzz.ratio

    def compute(self, prompts, predictions, references):
        return np.mean([self.metric(p, r) for p, r in zip(predictions, references)])


class RulerStringMatch(Metric):
    """
    Metric used in RULER.
    Reference: https://github.com/hsiehjackson/RULER/blob/main/scripts/eval/synthetic/constants.py
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @staticmethod
    def postprocess_pred(predict_str: str):
        predict_str = predict_str.strip()

        # Remove all non-printable characters
        np_pattern = re.compile(r"[\x00-\x1f]")
        predict_str = np_pattern.sub("\n", predict_str).strip()

        return predict_str

    @staticmethod
    def string_match_part(refs, preds):
        scores = [
            max([1.0 if r.lower() in pred.lower() else 0.0 for r in ref])
            for pred, ref in zip(preds, refs)
        ]
        score = sum(scores) / len(preds) * 100
        return {"score": round(score, 4)}

    @staticmethod
    def string_match_all(refs, preds):
        scores = [
            sum([1.0 if r.lower() in pred.lower() else 0.0 for r in ref]) / len(ref)
            for pred, ref in zip(preds, refs)
        ]
        score = sum(scores) / len(preds) * 100
        return {"score": round(score, 4)}

    def _load_metric(self, **kwargs):
        if kwargs.get("match_part", False):
            self.metric = self.string_match_part
        else:
            self.metric = self.string_match_all

    def compute(self, prompts, predictions, references):
        predictions = [self.postprocess_pred(pred) for pred in predictions]
        return self.metric(references, predictions)


REFERENCE_TEMPLATE = """You are shown ground-truth answer(s) and asked to judge the quality of an LLM-generated answer.
Assign it a score from 1-5 where 1 is the worst and 5 is the best based on how similar it is to the ground-truth(s).
Do NOT explain your choice. Simply return a number from 1-5.

====GROUND TRUTHS====
{labels}

====ANSWER====
{prediction}"""

PREFILL = "The score (1-5) is:"


class LLMRouge(Metric):
    def __init__(self, num_retries=5, **kwargs) -> None:
        assert (
            "ANTHROPIC_API_KEY" in os.environ
        ), "Please set the ANTHROPIC_API_KEY environment variable."
        super().__init__(**kwargs)
        self.num_retries = num_retries

    def _load_metric(self, **kwargs):
        name = kwargs.get("name", "haiku")
        matching_names = [m for m in models if name in m]
        assert len(matching_names) > 0, f"Model name {name} not found in {models}"
        assert (
            len(matching_names) == 1
        ), f"Model name {name} found x{len(matching_names)} in {models}"
        self.chat = Chat(
            matching_names[0], sp="""You are a helpful and concise assistant."""
        )

    def parse_int(self, text):
        return int(re.search(r"\d+", text).group())

    def compute(self, prompts, predictions, labels):
        scores = []
        for p, ls in zip(predictions, labels):
            prompt = REFERENCE_TEMPLATE.format(labels="\n---\n".join(ls), prediction=p)
            # Clear conversation history
            self.chat.h = []
            try:
                score = (
                    self.chat(prompt, prefill=PREFILL)
                    .content[0]
                    .text[len(PREFILL) :]
                    .strip()
                )
            except RateLimitError:
                retries = 0
                while retries < self.num_retries:
                    time.sleep(10)
                    try:
                        score = (
                            self.chat(prompt, prefill=PREFILL)
                            .content[0]
                            .text[len(PREFILL) :]
                            .strip()
                        )
                        break
                    except RateLimitError:
                        retries += 1
                if retries == self.num_retries:
                    raise RateLimitError("Exceeded maximum number of retries.")

            score = self.parse_int(score)
            scores.append(score)
        return {"llm_rouge": sum(scores) / len(scores)}


LLM_JUDGE_TEMPLATE = """You are shown a prompt and asked to assess the quality of an LLM-generated answer on the following dimensions:

===CRITERIA===
{criteria}

Respond with "criteria: score" for each criteria with a newline for each criteria.
Assign a score from 1-5 where 1 is the worst and 5 is the best based on how well the answer meets the criteria.

====PROMPT====
{prompt}

====ANSWER====
{prediction}"""


CRITERIA = {
    "helpful": "The answer executes the action requested by the prompt without extraneous detail.",
    "coherent": "The answer is logically structured and coherent (ignore the prompt).",
    "faithful": "The answer is faithful to the prompt and does not contain false information.",
}


class LLMJudge(LLMRouge):
    def __init__(self, **kwargs) -> None:
        assert (
            "ANTHROPIC_API_KEY" in os.environ
        ), "Please set the ANTHROPIC_API_KEY environment variable."
        super().__init__(**kwargs)

        self.criteria = list(sorted([k for k in CRITERIA]))
        self.criteria_def = "\n".join([f"{k}: {CRITERIA[k]}" for k in self.criteria])
        self.prefill = (
            f"\n\n====SCORES for {', '.join(self.criteria)}====\n\n{self.criteria[0]}:"
        )

    def parse_scorecard(self, scorecard):
        try:
            return {
                k: int(v)
                for k, v in dict(
                    re.findall(rf"({'|'.join(self.criteria)})\W+(\d+)", scorecard)
                ).items()
            }
        except Exception as e:
            print(e)
            raise Exception(
                f"Could not parse LLM-generated scorecard for {self.__class__}:\n{scorecard}"
            )

    def claudette_scorecard(self, prompt, prediction):
        prompt = LLM_JUDGE_TEMPLATE.format(
            criteria=self.criteria_def, prompt=prompt, prediction=prediction
        )
        # Clear conversation history
        self.chat.h = []
        scorecard = (
            self.chat(prompt, prefill=self.prefill)
            .content[0]
            .text[len(self.prefill) - len(self.criteria[0]) - 1 :]
            .strip()
        )
        return scorecard

    def compute(self, prompts, predictions, labels):
        scores = []

        for prompt, pred in zip(prompts, predictions):
            scorecard = self.claudette_scorecard(prompt, pred)
            score_dict = self.parse_scorecard(scorecard)
            scores.append(score_dict)

        return {k: np.mean([s[k] for s in scores]) for k in self.criteria}


METRIC_MAPPING = {
    "accuracy": Accuracy,
    "bertscore": BertScore,
    "bleurt": Bleurt,
    "exact_match": ExactMatchScore,
    "levenshtein": LevenshteinDistance,
    "llm-rouge": LLMRouge,
    "llm-as-a-judge": LLMJudge,
    "rouge": Rouge,
    "ruler-string-match": RulerStringMatch,
}


class AutoMetric:
    def __init__(self):
        raise EnvironmentError(
            "This class is designed to be instantiated only through the from_name method"
        )

    def from_name(metric_name, **kwargs):
        if metric_name not in METRIC_MAPPING:
            raise ValueError(f"Invalid metric name: {metric_name}")
        return METRIC_MAPPING[metric_name](**kwargs)


if __name__ == "__main__":
    metric = AutoMetric.from_name("llm-as-a-judge")
    predictions = [
        "The answer to 2x2 is 4.",
        "The answer to 2x2 is 5.",
    ]
    labels = [["4"], ["4"]]
    prompts = [
        "What is 2x2?",
        "What is 2x2?",
    ]
    print(metric.compute(prompts=prompts, predictions=predictions, labels=None))