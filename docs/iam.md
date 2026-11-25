# 最小 IAM 权限

## 本机控制器（运行 `ec2_gacha.py` 的身份）

把 `ACCOUNT_ID`、`ec2-ssm-probe-role` 替换成你的值。终止、改标签只允许作用于 `Project=aws_ec2_select` 的实例。

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Describe",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeSubnets",
        "ec2:DescribeAvailabilityZones",
        "ssm:DescribeInstanceInformation",
        "ssm:GetCommandInvocation"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Launch",
      "Effect": "Allow",
      "Action": "ec2:RunInstances",
      "Resource": "*"
    },
    {
      "Sid": "TagOnLaunch",
      "Effect": "Allow",
      "Action": "ec2:CreateTags",
      "Resource": "*",
      "Condition": { "StringEquals": { "ec2:CreateAction": "RunInstances" } }
    },
    {
      "Sid": "ManageOwnInstances",
      "Effect": "Allow",
      "Action": ["ec2:TerminateInstances", "ec2:CreateTags", "ec2:DeleteTags"],
      "Resource": "arn:aws:ec2:*:ACCOUNT_ID:instance/*",
      "Condition": { "StringEquals": { "aws:ResourceTag/Project": "aws_ec2_select" } }
    },
    {
      "Sid": "PassInstanceRole",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::ACCOUNT_ID:role/ec2-ssm-probe-role"
    },
    {
      "Sid": "PublicAmiParameter",
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": "arn:aws:ssm:*::parameter/aws/service/ami-amazon-linux-latest/*"
    },
    {
      "Sid": "RunShellOnOwnInstances",
      "Effect": "Allow",
      "Action": "ssm:SendCommand",
      "Resource": [
        "arn:aws:ssm:*::document/AWS-RunShellScript",
        "arn:aws:ec2:*:ACCOUNT_ID:instance/*"
      ],
      "Condition": { "StringEquals": { "aws:ResourceTag/Project": "aws_ec2_select" } }
    }
  ]
}
```

注意：`RunShellOnOwnInstances` 中的标签条件对 document 资源不生效时，可把 document ARN 拆成单独一条无条件的语句。

## 实例角色（`--instance-profile`）

- 托管策略 `AmazonSSMManagedInstanceCore`，不需要其他权限。
- 信任策略主体为 `ec2.amazonaws.com`。
