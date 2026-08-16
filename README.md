## About the repository

This is a eval set for testing the capabilities of Qwen 3.8 27B

Some evals are done on derived specs of genuine products I have implemented in the past. These existing products are considered the model product, however they do not serve as the ground truth. The models being tested are free to make their own architectual decisions, some of which may be more robust and sound than the decisions found in the existing products.

Model IDs in the eval sets are ananoymized to help account for bias. The model identities are revealed only after review & analysis.

Additionally a deterministic garder script grades each models submissions in a non-biased way by grading strictly on functionality.

LLMs GPT Sol and Fable 5 are used in conjunction for qualatative analyses and are expected to cross reference each other in order to deter same model family affinity. 

## Interpreting the results

From the current evals, Qwen is roughly 4.6 level on agentic engineering tasks and high level planning. Of course, this doesnt mean Qwen is the >= 4.6 at everything but for these sepcific eval sets, its roughly on par. I found Opus is better at choosing the more correct architecture, however it usually falls short in executing and assuring all invariants. This is genuinely surprising and I am seriously considering running this model locally. It could possibly pair well with a larger smarter model writing the specs.

## Repository status

This is an evolving working set. New projects, submissions, grader improvements, and review notes may be added over time, so conclusions can change as the evaluation suite becomes broader and more demanding.
