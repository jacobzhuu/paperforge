# 投毒攻击对序列推荐的影响
key=s2 level=1

## paragraph 1
- 注入伪造交互序列能够系统性地改变序列推荐模型的下一项预测，使目标物品的排名显著上升。
  cite_keys=['chen2023poison']
  evidence_ids=['e-1']
- 在MovieLens-1M数据集上，仅注入0.5%的合成用户画像，目标物品的命中率就从0.7%提升至14.2%，表明攻击者可以通过少量精心构造的交互序列操纵推荐结果。
  cite_keys=['chen2023poison']
  evidence_ids=['e-1']
- 这种攻击利用了序列推荐模型对用户行为序列的依赖，通过向训练数据中插入包含目标物品的伪造序列，使模型学习到虚假的共现模式，从而在预测阶段更倾向于推荐目标物品。
  cite_keys=['chen2023poison']
  evidence_ids=['e-1']
- 由于序列推荐通常采用留一法评估，即从用户交互历史中切分最后一项作为测试目标，攻击者注入的伪造序列会直接干扰模型对真实用户行为的建模。
  cite_keys=['wang2022seqrec']
  evidence_ids=['e-3']
- 因此，投毒攻击对序列推荐的影响不仅限于提升单个物品的曝光率，还可能扭曲整个推荐系统的排序逻辑，降低用户体验和系统可信度。
  cite_keys=['chen2023poison', 'wang2022seqrec']
  evidence_ids=['e-1', 'e-3']

## paragraph 2
- 针对投毒攻击的检测方法通常依赖交互序列的熵特征，通过识别异常的用户行为模式来区分真实用户和注入的伪造画像。
  cite_keys=['liu2024defense']
  evidence_ids=['e-2']
- 在3%的假阳性率下，基于熵的检测器能够召回91%的注入画像，显示出对较高比例注入攻击的有效性。
  cite_keys=['liu2024defense']
  evidence_ids=['e-2']
- 然而，当注入比例低于0.2%时，检测召回率骤降至38%，表明现有方法对低频注入攻击的敏感性不足。
  cite_keys=['liu2024defense']
  evidence_ids=['e-2']
- 因此，基于熵特征的检测手段存在明显的适用边界，在攻击者采用少量注入策略时可能失效，需要结合其他信号或更鲁棒的检测机制。
  cite_keys=['liu2024defense']
  evidence_ids=['e-2']

word_count=471
citation_warnings=[]
