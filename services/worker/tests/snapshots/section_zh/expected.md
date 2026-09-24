# 投毒攻击对序列推荐的影响
key=s2 level=1

## paragraph 1
- 注入式投毒攻击通过伪造用户画像与交互序列污染训练数据，能够系统性地抬高目标物品在下一项预测中的排名，这是当前文献中最一致的结论。
  cite_keys=['chen2023poison', 'liu2024defense']
  evidence_ids=['e-1']
- 其作用机制在于：序列推荐模型以用户历史交互的 leave-one-out 划分作为评估基准，伪造交互一旦混入训练集，就会被模型当作真实偏好模式学习，从而改变下一项预测的概率分布。
  cite_keys=['chen2023poison', 'wang2022seqrec']
  evidence_ids=['e-1', 'e-3']
- 攻击所需的注入成本极低：在 MovieLens-1M 上仅注入 0.5% 的合成用户画像，就能使目标物品的命中率从 0.7% 升至 14.2%，显示出放大数十倍的效果。
  cite_keys=['chen2023poison']
  evidence_ids=['e-1']
- 这说明即使攻击者控制的账号占比很小，也足以显著扭曲推荐结果，攻击效果与注入比例并非线性关系，而是利用了模型对协同信号的放大作用。
  cite_keys=['chen2023poison']
  evidence_ids=['e-1']

## paragraph 2
- 现有检测手段主要依赖交互熵等统计特征识别异常用户画像，但在低注入比例下检测能力显著退化，构成防御的明确边界。
  cite_keys=['liu2024defense']
  evidence_ids=['e-2']
- 熵检测器的原理是：投毒画像为最大化目标物品曝光，往往呈现与真实用户不同的交互集中度或模式化序列，从而在熵统计上偏离正常分布。
  cite_keys=['liu2024defense']
  evidence_ids=['e-2']
- 该检测器在较高注入比例下表现良好，能以 3% 的误报率召回 91% 的注入画像。
  cite_keys=['liu2024defense']
  evidence_ids=['e-2']
- 然而当注入比例低于 0.2% 时，召回率骤降至 38%，意味着攻击者只需将注入量控制在足够低的水平即可绕过检测。
  cite_keys=['liu2024defense']
  evidence_ids=['e-2']
- 结合攻击端与防御端的结果可以看出，当前防御与攻击之间存在不对称：0.5% 左右的注入已足以大幅抬升目标物品排名，而检测在该量级附近才开始有效，更低量级的隐蔽注入仍是开放问题。
  cite_keys=['chen2023poison', 'liu2024defense']
  evidence_ids=['e-1', 'e-2']

word_count=499
citation_warnings=[]
